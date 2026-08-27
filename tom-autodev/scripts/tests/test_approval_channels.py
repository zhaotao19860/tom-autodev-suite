import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infoflow-gateway"))

from approval_ledger import ApprovalLedger
from clients.infoflow_approval_client import InfoflowApprovalClient
from gateway import PendingApprovalGateway
from state_store import StateStore


class FakeApprovalTransport:
    def __init__(self):
        self.requests = []
        self.waits = []

    def request(self, request):
        self.requests.append(request)
        return {
            "request_id": "infoflow-request-1",
            "approval_id": request["approval_id"],
            "run_id": request.get("run_id", "run-1"),
            "channel": request.get("channel", "infoflow"),
            "input_hash": request["input_hash"],
            "member_policy": request.get(
                "member_policy",
                {
                    "comate": ["owner@example.test"],
                    "infoflow": ["owner@example.test"],
                },
            ),
            "status": "PENDING",
            "created_at": "2026-08-11T00:00:00+00:00",
            "deadline_at": request.get("deadline_at", "2026-08-11T10:00:00+00:00"),
            "heartbeat_at": None,
            "updated_at": None,
            "reply": None,
            "handoff": None,
            "reason_code": None,
        }

    def wait(self, request_id, timeout_seconds):
        self.waits.append((request_id, timeout_seconds))
        return {
            "request_id": request_id,
            "approval_id": "shared-1",
            "run_id": "run-1",
            "channel": "infoflow",
            "input_hash": "hash-a",
            "member_policy": {
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            },
            "status": "PENDING",
            "created_at": "2026-08-11T00:00:00+00:00",
            "deadline_at": "2026-08-11T10:00:00+00:00",
            "heartbeat_at": None,
            "updated_at": None,
            "reply": None,
            "handoff": None,
            "reason_code": None,
        }


class ApprovalLedgerChannelTests(unittest.TestCase):
    def test_infoflow_client_rejects_a_partial_gateway_request_result(self):
        class PartialTransport:
            def request(self, _request):
                return {"request_id": "partial-request"}

        client = InfoflowApprovalClient(PartialTransport())

        with self.assertRaisesRegex(ValueError, "APPROVAL_REQUEST_RESPONSE_INVALID"):
            client.request({"approval_id": "shared-1", "input_hash": "hash-a"})

    def test_infoflow_client_binds_complete_pending_result_to_every_immutable_request_field(self):
        request = {
            "run_id": "run-1",
            "approval_id": "shared-1",
            "channel": "infoflow",
            "input_hash": "hash-a",
            "member_policy": {
                "comate": ["z@example.test", "a@example.test"],
                "infoflow": ["z@example.test", "a@example.test"],
            },
            "deadline_at": "2026-08-11T10:00:00+08:00",
        }
        mismatches = {
            "run_id": "run-2",
            "approval_id": "shared-2",
            "channel": "comate",
            "input_hash": "hash-b",
            "member_policy": {
                "comate": ["a@example.test", "z@example.test"],
                "infoflow": ["attacker@example.test"],
            },
            "deadline_at": "2026-08-11T02:00:01+00:00",
        }

        for field, mismatched_value in mismatches.items():
            with self.subTest(field=field):
                class CompleteMismatchedTransport:
                    def request(self, _request):
                        return {
                            "request_id": "infoflow-request-1",
                            "approval_id": "shared-1",
                            "run_id": "run-1",
                            "channel": "infoflow",
                            "input_hash": "hash-a",
                            "member_policy": {
                                "comate": ["a@example.test", "z@example.test"],
                                "infoflow": ["a@example.test", "z@example.test"],
                            },
                            "status": "PENDING",
                            "created_at": "2026-08-11T00:00:00+00:00",
                            "deadline_at": "2026-08-11T02:00:00+00:00",
                            "heartbeat_at": None,
                            "updated_at": None,
                            "reply": None,
                            "handoff": None,
                            "reason_code": None,
                            field: mismatched_value,
                        }

                client = InfoflowApprovalClient(CompleteMismatchedTransport())

                with self.assertRaisesRegex(
                    ValueError, "APPROVAL_REQUEST_RESPONSE_INVALID"
                ):
                    client.request(request)

    def test_infoflow_client_accepts_order_normalized_policy_and_normalized_deadline(self):
        class MatchingTransport:
            def request(self, _request):
                return {
                    "request_id": "infoflow-request-1",
                    "approval_id": "shared-1",
                    "run_id": "run-1",
                    "channel": "infoflow",
                    "input_hash": "hash-a",
                    "member_policy": {
                        "comate": ["a@example.test", "z@example.test"],
                        "infoflow": ["a@example.test", "z@example.test"],
                    },
                    "status": "PENDING",
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "deadline_at": "2026-08-11T02:00:00Z",
                    "heartbeat_at": None,
                    "updated_at": None,
                    "reply": None,
                    "handoff": None,
                    "reason_code": None,
                }

        client = InfoflowApprovalClient(MatchingTransport())
        result = client.request(
            {
                "run_id": "run-1",
                "approval_id": "shared-1",
                "channel": "infoflow",
                "input_hash": "hash-a",
                "member_policy": {
                    "comate": ["z@example.test", "a@example.test"],
                    "infoflow": ["z@example.test", "a@example.test"],
                },
                "deadline_at": "2026-08-11T10:00:00+08:00",
            }
        )

        self.assertEqual(result["request_id"], "infoflow-request-1")

    def test_infoflow_client_rejects_each_matching_pending_gateway_error(self):
        request = {
            "run_id": "run-1",
            "approval_id": "shared-1",
            "channel": "infoflow",
            "input_hash": "hash-a",
            "member_policy": {
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            },
            "deadline_at": "2026-08-11T10:00:00+00:00",
        }
        gateway_errors = (
            "APPROVAL_ENVELOPE_INVALID",
            "APPROVAL_INPUT_MISMATCH",
            "APPROVAL_RESPONDER_UNAUTHORIZED",
            "APPROVAL_DECISION_INVALID",
        )

        for reason_code in gateway_errors:
            with self.subTest(reason_code=reason_code):
                class GatewayErrorTransport:
                    def request(self, _request):
                        return {
                            "request_id": "infoflow-request-1",
                            "approval_id": "shared-1",
                            "run_id": "run-1",
                            "channel": "infoflow",
                            "input_hash": "hash-a",
                            "member_policy": {
                                "comate": ["owner@example.test"],
                                "infoflow": ["owner@example.test"],
                            },
                            "status": "PENDING",
                            "created_at": "2026-08-11T00:00:00+00:00",
                            "deadline_at": "2026-08-11T10:00:00+00:00",
                            "heartbeat_at": None,
                            "updated_at": None,
                            "reply": None,
                            "handoff": None,
                            "reason_code": reason_code,
                        }

                client = InfoflowApprovalClient(GatewayErrorTransport())

                with self.assertRaisesRegex(
                    ValueError, "APPROVAL_REQUEST_RESPONSE_INVALID"
                ):
                    client.request(request)

    def test_first_valid_member_response_wins_and_conflict_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request(
                "G4",
                "hash-a",
                ["comate", "infoflow"],
                member_policy={"comate": ["dev@example.test"], "infoflow": ["owner@example.test"]},
            )
            accepted = ledger.resolve(
                request["approval_id"], "APPROVE", "hash-a", "comate", responder="dev@example.test"
            )
            late = ledger.resolve(
                request["approval_id"], "REJECT", "hash-a", "infoflow", responder="owner@example.test"
            )

            audit = ledger.responses(request["approval_id"])

        self.assertEqual(accepted["effective_decision"], "APPROVE")
        self.assertEqual(late["effective_decision"], "APPROVE")
        self.assertTrue(late["conflict"])
        self.assertEqual([(row["effective"], row["late"], row["responder"]) for row in audit], [(True, False, "dev@example.test"), (False, True, "owner@example.test")])

    def test_channel_member_and_malformed_decision_are_rejected_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            request = ledger.request(
                "G5",
                "hash-a",
                ["comate", "infoflow"],
                member_policy={"comate": ["dev@example.test"], "infoflow": ["owner@example.test"]},
            )

            unrequested = ledger.receive(
                request["approval_id"], "APPROVE", "hash-a", "other", "dev@example.test"
            )
            unknown_member = ledger.receive(
                request["approval_id"], "APPROVE", "hash-a", "comate", "stranger@example.test"
            )
            malformed = ledger.receive(
                request["approval_id"], "MAYBE", "hash-a", "comate", "dev@example.test"
            )

            status = ledger.get(request["approval_id"])["status"]
            rejected = [row["rejected_reason"] for row in ledger.responses(request["approval_id"])]

        self.assertEqual(unrequested["reason_code"], "APPROVAL_CHANNEL_NOT_CONFIGURED")
        self.assertEqual(unknown_member["reason_code"], "APPROVAL_RESPONDER_UNAUTHORIZED")
        self.assertEqual(malformed["reason_code"], "APPROVAL_DECISION_INVALID")
        self.assertEqual(status, "PENDING")
        self.assertEqual(rejected, ["APPROVAL_CHANNEL_NOT_CONFIGURED", "APPROVAL_RESPONDER_UNAUTHORIZED", "APPROVAL_DECISION_INVALID"])

    def test_timeout_is_a_stable_handoff_and_a_late_response_cannot_change_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            request = ledger.request("G6", "hash-a", ["comate", "infoflow"], deadline_at=deadline)

            timeout = ledger.timeout(request["approval_id"], "hash-a")
            late = ledger.resolve(request["approval_id"], "APPROVE", "hash-a", "comate")

            late_audited = ledger.responses(request["approval_id"])[0]["late"]

        self.assertEqual(timeout["reason_code"], "APPROVAL_TIMEOUT")
        self.assertEqual(timeout["handoff"]["status"], "PENDING")
        self.assertEqual(late["effective_decision"], "TIMEOUT")
        self.assertTrue(late_audited)

    def test_infoflow_client_uses_an_injected_transport_and_preserves_request_id(self):
        transport = FakeApprovalTransport()
        client = InfoflowApprovalClient(transport)

        requested = client.request(
            {
                "run_id": "run-1",
                "approval_id": "shared-1",
                "channel": "infoflow",
                "input_hash": "hash-a",
                "member_policy": {
                    "comate": ["owner@example.test"],
                    "infoflow": ["owner@example.test"],
                },
                "deadline_at": "2026-08-11T10:00:00+00:00",
            }
        )
        waiting = client.wait("infoflow-request-1", 10)

        self.assertEqual(requested["request_id"], "infoflow-request-1")
        self.assertEqual(waiting["status"], "PENDING")
        self.assertEqual(transport.requests[0]["approval_id"], "shared-1")
        self.assertEqual(transport.waits, [("infoflow-request-1", 10)])

    def test_infoflow_client_accepts_orchestrator_envelope_and_forwards_gateway_fields(self):
        transport = FakeApprovalTransport()
        client = InfoflowApprovalClient(transport)

        result = client.request(
            {
                "channel": "infoflow",
                "approval": {
                    "run_id": "run-1",
                    "approval_id": "shared-1",
                    "input_hash": "hash-a",
                    "member_policy": {
                        "comate": ["owner@example.test"],
                        "infoflow": ["owner@example.test"],
                    },
                    "deadline_at": "2026-08-11T10:00:00+00:00",
                },
            }
        )

        self.assertEqual(result["request_id"], "infoflow-request-1")
        self.assertEqual(transport.requests[0]["channel"], "infoflow")
        self.assertEqual(transport.requests[0]["run_id"], "run-1")


class PendingGatewayTests(unittest.TestCase):
    def test_gateway_timeout_and_heartbeat_do_not_extend_the_bound_deadline(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            gateway = PendingApprovalGateway(clock=lambda: now, state_store=StateStore(Path(directory) / "state.sqlite"))
            created = gateway.request({
                "request_id": "approval-1", "run_id": "run-1", "approval_id": "approval-1",
                "channel": "infoflow", "input_hash": "hash-a", "deadline_at": (now + timedelta(seconds=1)).isoformat(),
                "member_policy": {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
            })

            heartbeat = gateway.heartbeat("approval-1", observed_at=now.isoformat())
            timed_out = gateway.wait("approval-1", 0, now=now + timedelta(seconds=2))
            reply = gateway.reply("approval-1", {
                "run_id": "run-1", "approval_id": "approval-1", "channel": "infoflow",
                "decision": "APPROVE", "input_hash": "hash-a", "responder": "owner@example.test",
            })

        self.assertEqual(heartbeat["deadline_at"], created["deadline_at"])
        self.assertEqual(timed_out["status"], "TIMEOUT")
        self.assertEqual(reply["status"], "TIMEOUT")


if __name__ == "__main__":
    unittest.main()
