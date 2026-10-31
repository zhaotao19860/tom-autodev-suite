"""Scope approval in the worker: D and C cards replace their intermediate gates."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

import scope_approval
from orchestrator import Orchestrator, _scope_request
from test_orchestrator import PROFILE, _requirement_snapshot, _write_profile

OWNER = "owner@example.test"
POLICY = {"comate": [OWNER], "infoflow": [OWNER]}
CONTEXT = {"member_policy": POLICY, "evidence": {"gate": {"summary": []}}}


class ScopeWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _write_profile(root, PROFILE)
        self.orch = Orchestrator(root)
        started = self.orch.start("BGW-1", "bgw", requirement_snapshot=_requirement_snapshot("BGW-1"),
                                  approval_mode="scope")
        self.run_id = started["run_id"]

    def _enter(self, state):
        self.orch.state.transition(self.run_id, state, {"previous_state": "x"})

    def _ask(self, gate, digest):
        with patch.object(self.orch, "request_infoflow_approval", side_effect=self._deliver):
            return _scope_request(self.orch, self.run_id, gate, digest, CONTEXT)

    def _deliver(self, run_id, action, input_hash, **kwargs):
        self.delivered = (action, input_hash, kwargs["evidence"]["gate"])
        return self.orch.approvals.request(action, input_hash, ["comate", "infoflow"],
                                           run_id=run_id, member_policy=POLICY)

    def test_intake_pins_scope_mode_into_g0(self):
        intake = self.orch.state.events(self.run_id)[0]["payload"]
        self.assertEqual((intake["approval_mode"], intake["intake_hash_version"]), ("scope", "v4"))

    def test_d_segment_passes_g1_g2_and_raises_one_card_at_g4(self):
        self._enter("GRILL")
        first = self._ask("G1", "a" * 64)
        self.assertEqual(first["effective_channel"], "scope-pending:D")
        self._enter("SPEC")
        self._ask("G2", "b" * 64)
        self._enter("WORKSPACE")
        card = self._ask("G4", "c" * 64)
        action, bound, gate = self.delivered
        self.assertEqual(action, "SCOPE:D")
        self.assertEqual([g for g, _ in gate["scope"]["gates"]], ["G1", "G2", "G4"])
        self.assertIsNone(card["effective_decision"])
        # Once the card is approved, every gate of the segment is a derived APPROVE.
        for channel in ("comate", "infoflow"):
            self.orch.approvals.record_delivery(card["approval_id"], channel, {"id": channel}, payload_hash=bound)
        self.orch.approvals.resolve(card["approval_id"], "APPROVE", bound, "comate",
                                    run_id=self.run_id, responder=OWNER)
        exit_row = self._ask("G4", "c" * 64)
        self.assertEqual(exit_row["effective_channel"], f"scope:{card['approval_id']}")
        rows = {r["action"]: r for r in self.orch.approvals.for_run(self.run_id)}
        self.assertTrue(rows["G1"]["effective_channel"].startswith("scope:"))

    def test_a_changed_artifact_needs_a_new_card(self):
        self._enter("GRILL")
        self._ask("G1", "a" * 64)
        self._enter("SPEC")
        self._ask("G2", "b" * 64)
        self._enter("WORKSPACE")
        self._ask("G4", "c" * 64)
        first = self.delivered[1]
        self._enter("SPEC")
        self._ask("G2", "d" * 64)
        self._enter("WORKSPACE")
        self._ask("G4", "c" * 64)
        self.assertNotEqual(self.delivered[1], first)

    def test_a_run_without_scope_mode_is_asked_per_gate(self):
        other = self.orch.start("BGW-1", "bgw", requirement_snapshot=_requirement_snapshot("BGW-1"),
                                fresh_token="t")["run_id"]
        self.orch.state.transition(other, "GRILL", {})
        self.assertIsNone(_scope_request(self.orch, other, "G1", "a" * 64, CONTEXT))

    def test_g7_outside_submit_and_a_repair_round_are_per_gate(self):
        self._enter("IPIPE")
        self.assertIsNone(scope_approval.segment(self.orch.state.events(self.run_id), "G7"))
        self.orch.state.transition(self.run_id, "PLAN", {"previous_state": "DIAGNOSE"})
        self.assertIsNone(scope_approval.segment(self.orch.state.events(self.run_id), "G4"))


if __name__ == "__main__":
    unittest.main()
