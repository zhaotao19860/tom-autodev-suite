"""A re-issued (suffixed) approval, once approved, must satisfy its bare gate."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_ledger import ApprovalLedger, gate_of, MAX_APPROVAL_RETRIES
from clients.icode_runtime import _approved_record as icode_approved_record
from clients.ipipe_runtime import _approved_record as ipipe_approved_record
from orchestrator import Orchestrator


class GateNormalizationTests(unittest.TestCase):
    def test_gate_of_drops_retry_suffix(self):
        self.assertEqual(gate_of("G7"), "G7")
        self.assertEqual(gate_of("G7#retry-1"), "G7")
        self.assertEqual(gate_of("G9#retry-12"), "G9")

    def _approved_reissued(self, ledger):
        # A timed-out G7 is re-issued as "G7#retry-1"; the operator approves that card.
        record = ledger.request("G7#retry-1", "h" * 64, ["comate", "infoflow"], run_id="run-1",
                                member_policy={c: ["owner@example.test"] for c in ("comate", "infoflow")})
        for channel in ("comate", "infoflow"):
            ledger.record_delivery(record["approval_id"], channel, {"request_id": f"r-{channel}"},
                                   payload_hash="h" * 64)
        ledger.resolve(record["approval_id"], "APPROVE", "h" * 64, "comate",
                       run_id="run-1", responder="owner@example.test")
        return record["approval_id"]

    def test_reissued_approval_satisfies_the_bare_gate_consumers(self):
        for approved_record in (icode_approved_record, ipipe_approved_record):
            with tempfile.TemporaryDirectory() as directory:
                ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
                approval_id = self._approved_reissued(ledger)
                # The consumer asks for the bare gate "G7"; the record's action is "G7#retry-1".
                result = approved_record(
                    ledger, {"approval_id": approval_id, "input_hash": "h" * 64},
                    run_id="run-1", action="G7", input_hash="h" * 64)
                self.assertIsNone(result, (approved_record.__module__, result))

    def test_a_different_gate_still_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            approval_id = self._approved_reissued(ledger)
            result = icode_approved_record(
                ledger, {"approval_id": approval_id, "input_hash": "h" * 64},
                run_id="run-1", action="G5", input_hash="h" * 64)
            self.assertEqual(result["reason_code"], "APPROVAL_GATE_MISMATCH")

    def test_automatic_reissue_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            orch = Orchestrator(Path(directory))
            # MAX_APPROVAL_RETRIES suffixed attempts already timed out; the next reissue
            # must stop rather than mint "#retry-(MAX+1)".
            timed_out = [{"approval_id": f"a-{i}", "action": action, "input_hash": "h" * 64,
                          "effective_decision": "TIMEOUT", "status": "TIMEOUT"}
                         for i, action in enumerate(
                             ["G0"] + [f"G0#retry-{n}" for n in range(1, MAX_APPROVAL_RETRIES + 1)])]
            orch.approvals.for_run = lambda run_id: list(timed_out)
            result = orch.reissue_infoflow_approval(
                "run-1", "G0", "h" * 64,
                member_policy={c: ["owner@example.test"] for c in ("comate", "infoflow")},
                infoflow_client=object())
            self.assertEqual(result["reason_code"], "APPROVAL_RETRY_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
