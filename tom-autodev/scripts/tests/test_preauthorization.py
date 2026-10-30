"""A standing grant answers a delivered gate of a test card; nothing else."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from orchestrator import Orchestrator, _apply_preauthorization, _continue_preauthorized
from test_orchestrator import PROFILE, _start, _write_profile

OWNER = "owner@example.test"
POLICY = {"comate": [OWNER], "infoflow": [OWNER]}


class PreauthorizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_profile(self.root, PROFILE)
        self.orch = Orchestrator(self.root)
        self.run_id = _start(self.orch)["run_id"]

    def _grant(self, **overrides):
        grant = {
            "grant_id": "test-grant", "card_id": "BGW-1", "project": "bgw",
            "gates": ["G1", "G9"], "branches": ["main"], "responder": OWNER,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            **overrides,
        }
        (self.root / "preauthorizations.json").write_text(json.dumps({"grants": [grant]}))

    def _delivered(self, action="G1", input_hash="a" * 64, channels=("comate", "infoflow")):
        row = self.orch.approvals.request(action, input_hash, ["comate", "infoflow"],
                                          run_id=self.run_id, member_policy=POLICY)
        for channel in channels:
            self.orch.approvals.record_delivery(row["approval_id"], channel, {"id": channel},
                                                payload_hash=input_hash)
        return self.orch.approvals.get(row["approval_id"])

    def test_a_matching_grant_approves_through_the_ledger_and_is_audited(self):
        self._grant()
        approval = self._delivered()
        result = _apply_preauthorization(self.orch, self.run_id, approval)
        self.assertEqual(result["preauthorized"], "test-grant")
        row = self.orch.approvals.get(approval["approval_id"])
        self.assertEqual(row["effective_decision"], "APPROVE")
        audit = self.orch.state.idempotency_result(
            f"approval.preauthorized:{self.run_id}:{approval['approval_id']}")
        self.assertEqual(audit["grant_id"], "test-grant")

    def test_no_grant_for_another_card_gate_branch_or_after_expiry(self):
        cases = [
            {"card_id": "BGW-2"},
            {"gates": ["G9"]},
            {"branches": ["other"]},
            {"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
            {"responder": "stranger@example.test"},
        ]
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                self._grant(**overrides)
                approval = self._delivered(input_hash=f"{index:064d}")
                result = _apply_preauthorization(self.orch, self.run_id, approval)
                self.assertNotIn("preauthorized", result)
                self.assertIsNone(self.orch.approvals.get(approval["approval_id"])["effective_decision"])

    def test_an_undelivered_gate_is_not_answered(self):
        self._grant()
        approval = self._delivered(channels=("comate",))
        result = _apply_preauthorization(self.orch, self.run_id, approval)
        self.assertNotIn("preauthorized", result)
        self.assertEqual(result["preauthorization_refused"], "APPROVAL_DELIVERY_INCOMPLETE")

    def test_without_a_grant_file_nothing_changes(self):
        approval = self._delivered()
        self.assertIs(_apply_preauthorization(self.orch, self.run_id, approval), approval)

    def test_the_command_continues_itself_after_each_preauthorized_gate(self):
        class Bridge:
            calls = 0

            def continue_run(self, run_id):
                Bridge.calls += 1
                return {"ok": True, "reason_code": "PARKED", "parked": "PRODUCER_WAIT"}

        first = {"preauthorized": "test-grant", "approval": {"action": "G1", "approval_id": "a1"}}
        with patch("orchestrator._deliver_worker_approval", side_effect=lambda _o, r: r):
            result = _continue_preauthorized(self.orch, Bridge(), self.run_id, first)
        self.assertEqual(Bridge.calls, 1)
        self.assertEqual(result["parked"], "PRODUCER_WAIT")
        self.assertEqual(result["preauthorized_gates"][0]["approval_id"], "a1")


if __name__ == "__main__":
    unittest.main()
