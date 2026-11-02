"""R11A-002: a 需修改 on the WORKSPACE G4 has no producer to redo, so it is refused."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from approval_ledger import ApprovalLedger, revise_supported
from state_store import StateStore

POLICY = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}


class WorkspaceReviseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")

    def _g4(self, state):
        self.state.transition("run-1", state, {})
        row = self.ledger.request("G4", "h" * 64, ["comate", "infoflow"], run_id="run-1", member_policy=POLICY)
        for channel in ("comate", "infoflow"):
            self.ledger.record_delivery(row["approval_id"], channel, {"id": channel}, payload_hash="h" * 64)
        return row["approval_id"]

    def test_workspace_g4_refuses_revise_and_stays_pending(self):
        approval_id = self._g4("WORKSPACE")
        self.assertFalse(revise_supported("G4", self.state, "run-1"))
        refused = self.ledger.receive(approval_id, "REVISE", "h" * 64, "comate", "owner@example.test",
                                      run_id="run-1", state_store=self.state)
        self.assertEqual(refused["reason_code"], "APPROVAL_REVISE_NOT_SUPPORTED")
        approved = self.ledger.receive(approval_id, "APPROVE", "h" * 64, "comate", "owner@example.test",
                                       run_id="run-1", state_store=self.state)
        self.assertEqual(approved["effective_decision"], "APPROVE")

    def test_plan_g4_still_accepts_revise(self):
        approval_id = self._g4("PLAN")
        accepted = self.ledger.receive(approval_id, "REVISE", "h" * 64, "comate", "owner@example.test",
                                       run_id="run-1", state_store=self.state)
        self.assertEqual(accepted["effective_decision"], "REVISE")


if __name__ == "__main__":
    unittest.main()
