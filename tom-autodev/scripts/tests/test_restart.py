"""Contract 3.4: restart-from checks the source's leftovers and commits atomically."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import restart
from orchestrator import Orchestrator
from test_orchestrator import PROFILE, _requirement_snapshot, _start, _write_profile


class RestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_profile(self.root, PROFILE)
        self.orch = Orchestrator(self.root)
        self.source = _start(self.orch)["run_id"]
        self.snapshot = _requirement_snapshot("BGW-1")

    def _submitted_cr(self, number="1001"):
        claim = self.orch.state.claim_intent(self.source, "icode.submit", f"icode:{number}", {"n": number})
        self.orch.state.receipt(claim["intent"]["intent_id"], {
            "ok": True, "change_number": number, "module": "bgw", "change_id": "I1",
        }, [f"icode-cr-{number}"])

    def test_a_clean_source_restarts_once_and_replays(self):
        result = restart.execute(self.orch, self.source, self.snapshot, reason="需求变更")
        self.assertEqual(result["reason_code"], "RESTARTED", result)
        target = result["target_run_id"]
        self.assertEqual(self.orch.status(self.source)["state"], "STOPPED")
        target_events = self.orch.state.events(target)
        self.assertEqual([event["state"] for event in target_events], ["INTAKE"])
        self.assertEqual(target_events[0]["payload"]["restart_of"], self.source)
        self.assertEqual(self.orch.state.lineage(target)["source_run_id"], self.source)
        again = restart.execute(self.orch, self.source, self.snapshot, reason="需求变更")
        self.assertEqual(again["reason_code"], "RESTART_REPLAY")
        self.assertEqual(again["target_run_id"], target)

    def test_an_unreceipted_write_blocks_and_nothing_is_written(self):
        self.orch.state.claim_intent(self.source, "ipipe.rerun", "ipipe.rerun:x", {"stage_build_id": "s"})
        before = len(self.orch.state.events(self.source))
        result = restart.execute(self.orch, self.source, self.snapshot, reason="x")
        self.assertEqual(result["reason_code"], "RESTART_SOURCE_UNRESOLVED")
        self.assertEqual(result["blockers"][0]["kind"], "intent")
        self.assertEqual(len(self.orch.state.events(self.source)), before)
        self.assertIsNone(self.orch.state.lineage(self.source))

    def test_a_side_channel_write_does_not_block(self):
        self.orch.state.claim_intent(self.source, "ku.document.create", "ku:x", {"doc": "d"})
        self.assertTrue(restart.plan(self.orch, self.source)["ok"])

    def test_cr_dispositions(self):
        self._submitted_cr()
        for status, options, ok, disposition in [
            ("NEW", {}, False, "OPEN_UNDECIDED"),
            ("NEW", {"keep_open_crs": True}, True, "KEPT_FOR_REFERENCE"),
            ("ABANDONED", {}, True, "ABANDONED_CONFIRMED"),
            ("MERGED", {}, False, "MERGED_UNDECIDED"),
            ("MERGED", {"accept_merged": True}, True, "MERGED_ACCEPTED"),
            (None, {"keep_open_crs": True}, False, "ABANDONED_UNKNOWN"),
        ]:
            with self.subTest(status=status, options=options):
                planned = restart.plan(self.orch, self.source, cr_status=lambda _cr, s=status: s, **options)
                self.assertEqual(planned["ok"], ok, planned)
                self.assertEqual(planned["lineage"]["crs"][0]["disposition"], disposition)

    def test_a_running_or_unreadable_build_blocks(self):
        self.orch.state.save_idempotency_result(f"ipipe.build-binding:{self.source}:b1", {"build_id": "b1"})
        for status, ok in [("RUNNING", False), (None, False), ("SUCC", True)]:
            with self.subTest(status=status):
                planned = restart.plan(self.orch, self.source, build_status=lambda _b, s=status: s)
                self.assertEqual(planned["ok"], ok, planned)

    def test_a_released_source_cannot_restart(self):
        self.orch.state.transition(self.source, "RELEASE_SUCCESS", {})
        self.assertEqual(restart.plan(self.orch, self.source)["reason_code"], "RESTART_SOURCE_RELEASED")

    def test_a_reason_is_required(self):
        self.assertEqual(restart.execute(self.orch, self.source, self.snapshot, reason=" ")["reason_code"],
                         "RESTART_REASON_REQUIRED")


if __name__ == "__main__":
    unittest.main()
