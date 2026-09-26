import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from state_store import StateStore
from work_card import refresh


class _Orchestrator:
    def __init__(self, state):
        self.state = state


class _Client:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send_work_card(self, **payload):
        self.sent.append(payload)
        if self.fail:
            raise ConnectionError("unknown remote outcome")
        return {"card_id": f"work-{payload['run_id']}", "created": len(self.sent) == 1}


def _snapshot(*, phase="IPIPE", module="bgwagent"):
    return {
        "run_id": "run-1", "current_phase": phase,
        "overall": {"done": 8, "total": 11},
        "tasks": {"done": 2, "total": 3, "current": "T3"},
        "repositories": [{"module": module, "status": "SUCCESS"}],
        "pipelines": {"done": 1, "total": 2, "status": "MONITORING"},
        "release": {"status": "WAITING", "waiting_for": [module]},
        "next_action": {"owner": "测试", "text": "等待发布"},
    }


class WorkCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(Path(self.temp.name) / "state.sqlite")
        self.orchestrator = _Orchestrator(self.state)
        self.client = _Client()
        self.current = _snapshot()
        self.group = patch("work_card.group_id_for_run", return_value="123")
        self.progress = patch("work_card.build_progress", side_effect=lambda *_: self.current)
        self.group.start()
        self.progress.start()
        self.addCleanup(self.group.stop)
        self.addCleanup(self.progress.stop)

    def test_same_card_is_refreshed_only_when_progress_changes(self):
        first = refresh(self.orchestrator, self.client, "run-1")
        same = refresh(self.orchestrator, self.client, "run-1")
        self.current = _snapshot(phase="RELEASE", module="x86bgw")
        next_phase = refresh(self.orchestrator, self.client, "run-1")
        self.assertEqual((first["reason_code"], same["reason_code"], next_phase["reason_code"]),
                         ("WORK_CARD_UPDATED", "WORK_CARD_UNCHANGED", "WORK_CARD_UPDATED"))
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.sent[0]["run_id"], self.client.sent[1]["run_id"])
        self.assertEqual(self.client.sent[1]["lines"][0], "阶段 RELEASE · 8/11")
        self.assertIn("x86bgw=SUCCESS", self.client.sent[1]["lines"][2])
        self.assertLessEqual(len(self.client.sent[1]["lines"]), 8)

    def test_unknown_delivery_blocks_later_updates_until_reconciled(self):
        self.client.fail = True
        first = refresh(self.orchestrator, self.client, "run-1")
        self.current = _snapshot(phase="RELEASE")
        second = refresh(self.orchestrator, self.client, "run-1")
        self.assertEqual(first["reason_code"], "WORK_CARD_QUERY_REQUIRED")
        self.assertEqual(second["reason_code"], "WORK_CARD_QUERY_REQUIRED")
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(first["intent_id"], second["intent_id"])

    def test_restart_and_return_to_an_older_snapshot_refresh_the_same_card(self):
        self.assertEqual(refresh(self.orchestrator, self.client, "run-1")["reason_code"],
                         "WORK_CARD_UPDATED")
        restarted = _Orchestrator(StateStore(Path(self.temp.name) / "state.sqlite"))
        self.assertEqual(refresh(restarted, self.client, "run-1")["reason_code"],
                         "WORK_CARD_UNCHANGED")
        self.current = _snapshot(phase="RELEASE")
        refresh(restarted, self.client, "run-1")
        self.current = _snapshot()
        self.assertEqual(refresh(restarted, self.client, "run-1")["reason_code"],
                         "WORK_CARD_UPDATED")
        self.assertEqual(len(self.client.sent), 3)
        self.assertTrue(all(item["run_id"] == "run-1" for item in self.client.sent))

    def test_multiple_modules_appear_on_one_card(self):
        self.current["repositories"].append({"module": "x86bgw", "status": "MONITORING"})
        self.current["release"]["waiting_for"] = ["bgwagent", "x86bgw"]
        refresh(self.orchestrator, self.client, "run-1")
        self.assertIn("bgwagent=SUCCESS", self.client.sent[0]["lines"][2])
        self.assertIn("x86bgw=MONITORING", self.client.sent[0]["lines"][2])
        self.assertIn("bgwagent,x86bgw", self.client.sent[0]["lines"][4])

    def test_missing_group_does_not_send(self):
        with patch("work_card.group_id_for_run", return_value=None):
            result = refresh(self.orchestrator, self.client, "run-1")
        self.assertEqual(result["reason_code"], "WORK_CARD_UNAVAILABLE")
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()
