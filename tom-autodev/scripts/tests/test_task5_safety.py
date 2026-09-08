import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infoflow-gateway"))

from approval_ledger import ApprovalLedger
from collaboration import CollaborationSession
from clients.infoflow_approval_client import InfoflowApprovalClient
from clients.infoflow_group_client import InfoflowGroupClient
from gateway import PendingApprovalGateway
from orchestrator import Orchestrator
from state_store import StateStore


class GroupFake:
    def __init__(self):
        self.create_calls = []
        self.send_calls = []
        self.lock = threading.Lock()

    def create_or_reuse(self, request):
        with self.lock:
            self.create_calls.append(request)
        return {"group_id": "41", "bot_id": "b"}

    def reconcile_group(self, request):
        return None

    def send_markdown(self, group_id, content, at_users, idempotency_key):
        with self.lock:
            self.send_calls.append((group_id, content, at_users, idempotency_key))
        return {"message_id": str(len(self.send_calls))}

    def reconcile_message(self, group_id, idempotency_key):
        return None


def roster():
    return {
        "development": [{"email": "dev@example.test"}],
        "test": ["test@example.test"],
        "project": [{"email": "owner@example.test"}],
        "icafe_responsible": {"project": ["icafe-owner@example.test"]},
        "allow_card_owners": True,
        "card_owners": [{"email": "card-owner@example.test"}],
    }


def requirement_snapshot():
    return {
        "canonical_card_id": "BGW-1",
        "title": "Actual requirement",
        "body": "Behavior body",
        "html": "<p>Behavior body</p>",
        "acceptance": ["AC-1"],
        "fields": {"Acceptance": "AC-1", "Priority": "P1"},
        "attachments": [{"id": "att-1", "url": "https://files.example.test/a"}],
        "links": ["https://example.test/spec"],
        "status": "New",
        "type": "Story",
        "responsible_people": [
            {"username": "dev", "name": "Developer", "email": "dev@example.test"}
        ],
        "created": {
            "user": {"username": "owner"},
            "time": "2026-08-01 10:00:00",
        },
        "modified": {
            "user": {"username": "dev"},
            "time": "2026-08-02 11:00:00",
        },
        "content_hash": "3e4685034a404de8f3859f3cc5530c9ad7da9531858bf565d8b0e83f1bbf658e",
    }


def strict_intake(state, run_id="run-1"):
    state.transition(
        run_id,
        "INTAKE",
        {
            "project": "bgw",
            "requirement_id": "BGW-1",
            "profile_hash": "profile-a",
            "collaboration_binding": {
                "card": {"id": "BGW-1", "title": "Recorded requirement", "content_hash": "a" * 64},
                "role_members": {
                    "development": ["dev@example.test"],
                    "test": ["test@example.test"],
                    "project": ["owner@example.test"],
                },
            },
        },
    )


def _pending_delivery_receipt(payload, request_id="infoflow-request"):
    approval = payload["approval"]
    return {
        "request_id": request_id,
        "approval_id": approval["approval_id"],
        "run_id": approval["run_id"],
        "channel": "infoflow",
        "input_hash": approval["input_hash"],
        "member_policy": approval["member_policy"],
        "status": "PENDING",
        "created_at": "2026-08-11T00:00:00+00:00",
        "deadline_at": approval["deadline_at"],
        "heartbeat_at": None,
        "updated_at": None,
        "reply": None,
        "handoff": None,
        "reason_code": None,
    }


class CollaborationSafetyTests(unittest.TestCase):
    def test_start_requires_a_snapshot_and_persists_the_complete_prepared_g0_binding(self):
        profile = {
            "project_id": "bgw",
            "approval_channels": {
                "role_members": {
                    "development": ["dev@example.test"],
                    "test": ["test@example.test"],
                    "project": ["owner@example.test"],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_file = root / "config" / "projects" / "bgw.yaml"
            profile_file.parent.mkdir(parents=True)
            profile_file.write_text("fixture", encoding="utf-8")
            with patch("orchestrator.load_profile", return_value={"ready": True, "profile": profile}):
                orchestrator = Orchestrator(root)
                missing = orchestrator.start("BGW-1", "bgw")
                started = orchestrator.start(
                    "BGW-1", "bgw", requirement_snapshot=requirement_snapshot()
                )
            payload = orchestrator.state.events(started["run_id"])[0]["payload"]
            binding = payload["collaboration_binding"]

        self.assertEqual(missing.get("reason_code"), "ICAFE_SNAPSHOT_REQUIRED")
        self.assertNotIn("run_id", missing)
        self.assertEqual(binding["run_id"], started["run_id"])
        self.assertEqual(binding["project"], "bgw")
        self.assertEqual(binding["card_id"], "BGW-1")
        self.assertEqual(binding["owner"], "owner@example.test")
        self.assertEqual(binding["roles"], {
            "development": ["dev@example.test"],
            "test": ["test@example.test"],
            "project": ["owner@example.test"],
        })
        self.assertEqual(
            binding["session_idempotency_key"],
            f"infoflow.group.create:{started['run_id']}",
        )
        self.assertNotIn("group_id", binding)
        prerequisites = {
            key: payload[key]
            for key in (
                "requirement_id", "project", "profile_hash", "requirement_snapshot",
                "collaboration_binding",
            )
        }
        self.assertEqual(payload["g0_input_hash"], hashlib.sha256(
            json.dumps(
                prerequisites, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest())

    def test_start_verifies_the_complete_cafe_snapshot_before_g0_uses_its_title(self):
        profile = {
            "project_id": "bgw",
            "approval_channels": {
                "role_members": {
                    "development": ["dev@example.test"],
                    "test": ["test@example.test"],
                    "project": ["owner@example.test"],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_file = root / "config" / "projects" / "bgw.yaml"
            profile_file.parent.mkdir(parents=True)
            profile_file.write_text("fixture", encoding="utf-8")
            with patch("orchestrator.load_profile", return_value={"ready": True, "profile": profile}):
                orchestrator = Orchestrator(root)
                missing = orchestrator.start("BGW-1", "bgw")
                canonical = orchestrator.start("BGW-1", "bgw", requirement_snapshot=requirement_snapshot())
                mismatched_snapshot = requirement_snapshot()
                mismatched_snapshot["title"] = "Forged title"
                mismatched = orchestrator.start(
                    "BGW-1", "bgw", requirement_snapshot=mismatched_snapshot
                )
                partial = orchestrator.start(
                    "BGW-1",
                    "bgw",
                    requirement_snapshot={
                        "canonical_card_id": "BGW-1",
                        "title": "Arbitrary title",
                        "content_hash": "a" * 64,
                    },
                )
            prepared = orchestrator.collaboration_session(GroupFake()).prepare_g0(
                canonical["run_id"], "bgw", {"id": "BGW-1", "title": "forged"}, roster()
            )

        self.assertEqual(missing["reason_code"], "ICAFE_SNAPSHOT_REQUIRED")
        self.assertNotIn("run_id", missing)
        self.assertEqual(mismatched["reason_code"], "ICAFE_SNAPSHOT_HASH_MISMATCH")
        self.assertEqual(partial["reason_code"], "ICAFE_SNAPSHOT_INVALID")
        self.assertEqual(prepared["request"]["group_name"], "BGW-1-Actual-requi")
        self.assertEqual(
            prepared["request"]["card_content_hash"],
            "3e4685034a404de8f3859f3cc5530c9ad7da9531858bf565d8b0e83f1bbf658e",
        )

    def test_orchestrator_session_is_ledger_backed_and_uses_immutable_profile_card_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            session = orchestrator.collaboration_session(GroupFake())
            prepared = session.prepare_g0(
                "run-1",
                "bgw",
                {"id": "BGW-1", "title": ""},
                {
                    "development": ["attacker@example.test"],
                    "test": ["attacker@example.test"],
                    "project": ["attacker@example.test"],
                },
            )

        self.assertIs(session.approvals, orchestrator.approvals)
        self.assertEqual(prepared["request"]["owner"], "owner@example.test")
        self.assertEqual(prepared["request"]["member_snapshot"], ["dev@example.test", "owner@example.test", "test@example.test"])
        self.assertEqual(prepared["request"]["group_name"], "BGW-1-Recorded-req")

    def test_g0_is_bound_to_existing_run_project_profile_card_and_canonical_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            strict_intake(state)
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            group = GroupFake()
            session = CollaborationSession(state, group, approvals=ledger)
            prepared = session.prepare_g0("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster())
            approval = ledger.request("G0", prepared["input_hash"], ["comate", "infoflow"], run_id="run-1", member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]})
            ledger.record_delivery(approval["approval_id"], "comate", {"request_id": "c"}, payload_hash=prepared["input_hash"])
            ledger.record_delivery(approval["approval_id"], "infoflow", {"request_id": "i"}, payload_hash=prepared["input_hash"])
            ledger.resolve(approval["approval_id"], "APPROVE", prepared["input_hash"], "comate", run_id="run-1", responder="owner@example.test")

            created = session.create("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])
            forged = session.create("run-1", "other", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])

        self.assertEqual(created["group_id"], "41")
        self.assertEqual(forged["reason_code"], "G0_BINDING_MISMATCH")
        self.assertEqual(len(group.create_calls), 1)

    def test_one_group_and_one_message_write_are_owned_atomically_across_concurrent_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            strict_intake(state)
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            group = GroupFake()
            first = CollaborationSession(state, group, approvals=ledger)
            prepared = first.prepare_g0("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster())
            approval = ledger.request("G0", prepared["input_hash"], ["comate", "infoflow"], run_id="run-1", member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]})
            ledger.record_delivery(approval["approval_id"], "comate", {"request_id": "c"}, payload_hash=prepared["input_hash"])
            ledger.record_delivery(approval["approval_id"], "infoflow", {"request_id": "i"}, payload_hash=prepared["input_hash"])
            ledger.resolve(approval["approval_id"], "APPROVE", prepared["input_hash"], "comate", run_id="run-1", responder="owner@example.test")

            def create_once(_):
                return CollaborationSession(StateStore(database), group, approvals=ApprovalLedger(Path(directory) / "approvals.sqlite")).create("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(create_once, range(8)))
            restarted = CollaborationSession(StateStore(database), group, approvals=ledger)
            repeated = restarted.create("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])
            sender = CollaborationSession(StateStore(database), group, approvals=ledger)
            sender.create("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])
            with ThreadPoolExecutor(max_workers=6) as pool:
                messages = list(pool.map(lambda _: sender.send_message("run-1", "@dev@example.test x", ["dev@example.test"], "same"), range(6)))

        self.assertEqual(len(group.create_calls), 1)
        self.assertEqual(repeated["group_id"], "41")
        self.assertEqual(len(group.send_calls), 1)
        self.assertTrue(all(result.get("group_id") == "41" or result.get("reason_code") == "QUERY_REQUIRED" for result in results))
        self.assertTrue(all(result.get("message_id") == "1" or result.get("reason_code") == "QUERY_REQUIRED" for result in messages))

    def test_owner_route_and_mentions_are_exact_and_untrusted_text_cannot_add_mentions(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            strict_intake(state)
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            group = GroupFake()
            session = CollaborationSession(state, group, approvals=ledger)
            prepared = session.prepare_g0("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster())
            approval = ledger.request("G0", prepared["input_hash"], ["comate", "infoflow"], run_id="run-1", member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]})
            ledger.record_delivery(approval["approval_id"], "comate", {"request_id": "c"}, payload_hash=prepared["input_hash"])
            ledger.record_delivery(approval["approval_id"], "infoflow", {"request_id": "i"}, payload_hash=prepared["input_hash"])
            ledger.resolve(approval["approval_id"], "APPROVE", prepared["input_hash"], "comate", run_id="run-1", responder="owner@example.test")
            session.create("run-1", "bgw", {"id": "BGW-1", "title": "Title"}, roster(), approval_id=approval["approval_id"], input_hash=prepared["input_hash"])
            routed = session.route_failure({"run_id": "run-1", "category": "auth", "summary": "ask @extra@example.test", "next_action": "call @bad@example.test"})
            mismatch = session.send_message("run-1", "@dev@example.test @extra@example.test", ["dev@example.test"], "bad")

        self.assertEqual(routed["at_users"], ["owner@example.test"])
        self.assertNotIn("@extra@example.test", routed["content"])
        self.assertEqual(mismatch["reason_code"], "MESSAGE_MENTION_MISMATCH")


class ApprovalSafetyTests(unittest.TestCase):
    def test_strict_response_and_cli_reject_omitted_run_or_responder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = ApprovalLedger(root / "approvals.sqlite")
            request = ledger.request(
                "G2", "hash", ["comate", "infoflow"], run_id="run-1",
                member_policy={"comate": ["dev@example.test"], "infoflow": ["owner@example.test"]},
            )
            ledger.record_delivery(request["approval_id"], "comate", {"request_id": "c"}, payload_hash="hash")
            ledger.record_delivery(request["approval_id"], "infoflow", {"request_id": "i"}, payload_hash="hash")
            omitted = ledger.receive(request["approval_id"], "APPROVE", "hash", "comate", "dev@example.test")
            process = __import__("subprocess").run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "orchestrator.py"), "--config-root", str(root), "approve", request["approval_id"], "APPROVE", "hash", "comate"],
                capture_output=True,
                text=True,
            )

        self.assertEqual(omitted["reason_code"], "APPROVAL_RUN_MISMATCH")
        self.assertEqual(process.returncode, 2)

    def test_legacy_approval_cannot_advance_a_run_bound_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            legacy = orchestrator.approvals.request("G0", "hash", ["comate", "infoflow"])
            orchestrator.approvals.resolve(legacy["approval_id"], "APPROVE", "hash", "comate")
            result = orchestrator.advance(
                "run-1",
                "GRILL",
                {"input_hash": "hash", "approval_id": legacy["approval_id"], "artifacts": ["requirement-snapshot", "collaboration-session"]},
            )

        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")

    def test_run_and_authenticated_member_policy_are_required_and_cross_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            policy = {"comate": ["dev@example.test"], "infoflow": ["owner@example.test"]}
            one = ledger.request("G2", "same", ["comate", "infoflow"], run_id="run-1", member_policy=policy)
            two = ledger.request("G2", "same", ["comate", "infoflow"], run_id="run-2", member_policy=policy)
            ledger.record_delivery(one["approval_id"], "comate", {"request_id": "c"}, payload_hash="same")
            ledger.record_delivery(one["approval_id"], "infoflow", {"request_id": "i"}, payload_hash="same")
            anonymous = ledger.receive(one["approval_id"], "APPROVE", "same", "comate", None, run_id="run-1")
            wrong_run = ledger.receive(one["approval_id"], "APPROVE", "same", "comate", "dev@example.test", run_id="run-2")
            accepted = ledger.receive(one["approval_id"], "APPROVE", "same", "comate", "dev@example.test", run_id="run-1")

        self.assertNotEqual(one["approval_id"], two["approval_id"])
        self.assertEqual(anonymous["reason_code"], "APPROVAL_RESPONDER_UNAUTHORIZED")
        self.assertEqual(wrong_run["reason_code"], "APPROVAL_RUN_MISMATCH")
        self.assertEqual(accepted["effective_decision"], "APPROVE")

    def test_deadline_is_closed_and_timeout_handoff_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite")
            state.transition("run-1", "INTAKE", {"project": "bgw", "requirement_id": "BGW-1", "profile_hash": "profile-a"})
            ledger = ApprovalLedger(root / "approvals.sqlite")
            policy = {"comate": ["dev@example.test"], "infoflow": ["owner@example.test"]}
            deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            request = ledger.request(
                "G3", "hash", ["comate", "infoflow"], run_id="run-1",
                member_policy=policy, deadline_at=deadline,
            )
            ledger.record_delivery(request["approval_id"], "comate", {"request_id": "c"}, payload_hash="hash")
            ledger.record_delivery(request["approval_id"], "infoflow", {"request_id": "i"}, payload_hash="hash")
            early = ledger.timeout(request["approval_id"], "hash", run_id="run-1", state_store=state)
            late = ledger.receive(request["approval_id"], "APPROVE", "hash", "comate", "dev@example.test", run_id="run-1", now=datetime.now(timezone.utc) + timedelta(minutes=6), state_store=state)
            timeout = ledger.timeout(request["approval_id"], "hash", run_id="run-1", state_store=state, now=datetime.now(timezone.utc) + timedelta(minutes=6))
            handoffs = state.incomplete_handoffs("run-1")

        self.assertEqual(early["reason_code"], "APPROVAL_TIMEOUT_EARLY")
        self.assertEqual(late["reason_code"], "APPROVAL_TIMEOUT")
        self.assertEqual(late["handoff"]["status"], "PENDING")
        self.assertEqual(timeout["reason_code"], "APPROVAL_ALREADY_RESOLVED")
        self.assertEqual(handoffs[0]["handoff_id"], late["handoff"]["handoff_id"])


class ClientAndOrchestratorSafetyTests(unittest.TestCase):
    def test_orchestrator_rejects_each_complete_mismatched_infoflow_delivery_receipt(self):
        class ComateClient:
            def request(self, _payload):
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        mismatches = {
            "run_id": "run-2",
            "approval_id": "other-approval",
            "channel": "comate",
            "input_hash": "other-hash",
            "member_policy": {
                "comate": ["a@example.test", "z@example.test"],
                "infoflow": ["attacker@example.test"],
            },
            "deadline_at": "2026-08-11T02:00:01+00:00",
        }
        policy = {
            "comate": ["z@example.test", "a@example.test"],
            "infoflow": ["z@example.test", "a@example.test"],
        }

        for field, mismatched_value in mismatches.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                class CompleteMismatchedTransport:
                    def __init__(self):
                        self.requests = []
                        self.reconciles = []

                    def request(self, request):
                        self.requests.append(request)
                        return {
                            "request_id": "infoflow-request",
                            "approval_id": request["approval_id"],
                            "run_id": "run-1",
                            "channel": "infoflow",
                            "input_hash": "hash",
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

                    def reconcile(self, request):
                        self.reconciles.append(request)
                        return None

                orchestrator = Orchestrator(Path(directory))
                strict_intake(orchestrator.state)
                transport = CompleteMismatchedTransport()
                requested = orchestrator.request_infoflow_approval(
                    "run-1",
                    "G0",
                    "hash",
                    deadline_at="2026-08-11T10:00:00+08:00",
                    member_policy=policy,
                    comate_client=ComateClient(),
                    infoflow_client=InfoflowApprovalClient(transport),
                )
                repeated = orchestrator.request_infoflow_approval(
                    "run-1",
                    "G0",
                    "hash",
                    deadline_at="2026-08-11T10:00:00+08:00",
                    member_policy=policy,
                    comate_client=ComateClient(),
                    infoflow_client=InfoflowApprovalClient(transport),
                )
                approval_id = requested["approval_id"]
                approval = orchestrator.approvals.get(approval_id)
                blocked = orchestrator.approvals.receive(
                    approval_id,
                    "APPROVE",
                    "hash",
                    "comate",
                    "a@example.test",
                    run_id="run-1",
                )

                self.assertEqual(
                    requested["reason_code"], "APPROVAL_REQUEST_RESPONSE_INVALID"
                )
                self.assertEqual(
                    repeated["reason_code"], "APPROVAL_REQUEST_RESPONSE_INVALID"
                )
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(transport.reconciles, [])
                self.assertEqual(
                    [entry["channel"] for entry in approval["delivery_receipts"]],
                    ["comate"],
                )
                # The gate is not waiting on a human: the Infoflow card was refused, so
                # nobody can answer it -- which is what APPROVAL_DELIVERY_INCOMPLETE
                # below says. The ledger reports that instead of a pending gate that
                # would sit there until its deadline expired.
                self.assertEqual(approval["status"], "DELIVERY_FAILED")
                self.assertEqual(approval["delivery_failure"]["channels"], ["infoflow"])
                self.assertFalse(approval["delivery_failure"]["retry_allowed"])
                self.assertIsNone(approval["effective_decision"])
                self.assertEqual(
                    blocked["reason_code"], "APPROVAL_DELIVERY_INCOMPLETE"
                )

    def test_orchestrator_rechecks_typed_infoflow_receipt_before_recording_it(self):
        class ComateClient:
            def request(self, _payload):
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        class RawInfoflowClient:
            def request(self, payload):
                approval = payload["approval"]
                return {
                    "request_id": "infoflow-request",
                    "approval_id": approval["approval_id"],
                    "run_id": approval["run_id"],
                    "channel": "infoflow",
                    "input_hash": approval["input_hash"],
                    "member_policy": approval["member_policy"],
                    "status": "PENDING",
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "deadline_at": "2026-08-11T02:00:01+00:00",
                    "heartbeat_at": None,
                    "updated_at": None,
                    "reply": None,
                    "handoff": None,
                    "reason_code": None,
                }

            def reconcile(self, _payload):
                raise AssertionError("a typed mismatch must not be reconciled")

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            requested = orchestrator.request_infoflow_approval(
                "run-1",
                "G0",
                "hash",
                deadline_at="2026-08-11T10:00:00+08:00",
                member_policy={
                    "comate": ["owner@example.test"],
                    "infoflow": ["owner@example.test"],
                },
                comate_client=ComateClient(),
                infoflow_client=RawInfoflowClient(),
            )
            approval = orchestrator.approvals.get(requested["approval_id"])

        self.assertEqual(
            requested["reason_code"], "APPROVAL_REQUEST_RESPONSE_INVALID"
        )
        self.assertEqual(
            [entry["channel"] for entry in approval["delivery_receipts"]], ["comate"]
        )

    def test_orchestrator_rejects_each_matching_pending_gateway_error_before_receipt(self):
        class ComateClient:
            def request(self, _payload):
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        gateway_errors = (
            "APPROVAL_ENVELOPE_INVALID",
            "APPROVAL_INPUT_MISMATCH",
            "APPROVAL_RESPONDER_UNAUTHORIZED",
            "APPROVAL_DECISION_INVALID",
        )
        policy = {
            "comate": ["owner@example.test"],
            "infoflow": ["owner@example.test"],
        }

        for reason_code in gateway_errors:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as directory:
                class RawGatewayErrorClient:
                    def __init__(self):
                        self.requests = []
                        self.reconciles = []

                    def request(self, payload):
                        self.requests.append(payload)
                        approval = payload["approval"]
                        return {
                            "request_id": "infoflow-request",
                            "approval_id": approval["approval_id"],
                            "run_id": approval["run_id"],
                            "channel": "infoflow",
                            "input_hash": approval["input_hash"],
                            "member_policy": approval["member_policy"],
                            "status": "PENDING",
                            "created_at": "2026-08-11T00:00:00+00:00",
                            "deadline_at": approval["deadline_at"],
                            "heartbeat_at": None,
                            "updated_at": None,
                            "reply": None,
                            "handoff": None,
                            "reason_code": reason_code,
                        }

                    def reconcile(self, payload):
                        self.reconciles.append(payload)
                        return None

                orchestrator = Orchestrator(Path(directory))
                strict_intake(orchestrator.state)
                infoflow = RawGatewayErrorClient()
                requested = orchestrator.request_infoflow_approval(
                    "run-1",
                    "G0",
                    "hash",
                    deadline_at="2026-08-11T10:00:00+00:00",
                    member_policy=policy,
                    comate_client=ComateClient(),
                    infoflow_client=infoflow,
                )
                repeated = orchestrator.request_infoflow_approval(
                    "run-1",
                    "G0",
                    "hash",
                    deadline_at="2026-08-11T10:00:00+00:00",
                    member_policy=policy,
                    comate_client=ComateClient(),
                    infoflow_client=infoflow,
                )
                approval_id = requested["approval_id"]
                approval = orchestrator.approvals.get(approval_id)
                blocked = orchestrator.approvals.receive(
                    approval_id,
                    "APPROVE",
                    "hash",
                    "comate",
                    "owner@example.test",
                    run_id="run-1",
                )

                self.assertEqual(
                    requested["reason_code"], "APPROVAL_REQUEST_RESPONSE_INVALID"
                )
                self.assertEqual(
                    repeated["reason_code"], "APPROVAL_REQUEST_RESPONSE_INVALID"
                )
                self.assertEqual(len(infoflow.requests), 1)
                self.assertEqual(infoflow.reconciles, [])
                self.assertEqual(
                    [entry["channel"] for entry in approval["delivery_receipts"]],
                    ["comate"],
                )
                # The gate is not waiting on a human: the Infoflow card was refused, so
                # nobody can answer it -- which is what APPROVAL_DELIVERY_INCOMPLETE
                # below says. The ledger reports that instead of a pending gate that
                # would sit there until its deadline expired.
                self.assertEqual(approval["status"], "DELIVERY_FAILED")
                self.assertEqual(approval["delivery_failure"]["channels"], ["infoflow"])
                self.assertFalse(approval["delivery_failure"]["retry_allowed"])
                self.assertIsNone(approval["effective_decision"])
                self.assertEqual(
                    blocked["reason_code"], "APPROVAL_DELIVERY_INCOMPLETE"
                )

    def test_real_gateway_client_orchestrator_chain_accepts_pending_and_nested_reply_once(self):
        class ComateTransport:
            def request(self, _payload):
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            gateway = PendingApprovalGateway(clock=lambda: now, state_store=orchestrator.state)
            infoflow = InfoflowApprovalClient(gateway)
            policy = {
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            }
            requested = orchestrator.request_infoflow_approval(
                "run-1",
                "G0",
                "hash",
                deadline_at=(now + timedelta(hours=24)).isoformat(),
                member_policy=policy,
                comate_client=ComateTransport(),
                infoflow_client=infoflow,
            )
            approval_id = requested["approval_id"]
            request_id = next(
                entry["receipt"]["request_id"]
                for entry in requested["delivery_receipts"]
                if entry["channel"] == "infoflow"
            )

            pending = orchestrator.wait_infoflow_approval(
                "run-1", approval_id, "hash", infoflow, 0
            )
            audit_after_pending = orchestrator.approvals.responses(approval_id)
            gateway.reply(
                request_id,
                {
                    "run_id": "run-1",
                    "approval_id": approval_id,
                    "channel": "infoflow",
                    "input_hash": "hash",
                    "decision": "APPROVE",
                    "responder": "owner@example.test",
                },
            )
            accepted = orchestrator.wait_infoflow_approval(
                "run-1", approval_id, "hash", infoflow, 0
            )
            repeated = orchestrator.wait_infoflow_approval(
                "run-1", approval_id, "hash", infoflow, 0
            )
            audit_after_repeated = orchestrator.approvals.responses(approval_id)

        self.assertEqual(pending["reason_code"], "PENDING")
        self.assertEqual(audit_after_pending, [])
        self.assertEqual(accepted["effective_decision"], "APPROVE")
        self.assertEqual(repeated["effective_decision"], "APPROVE")
        self.assertEqual(len(audit_after_repeated), 1)

    def test_real_chain_reuses_timeout_handoff_and_audits_malformed_gateway_record(self):
        class ComateTransport:
            def request(self, payload):
                if payload["channel"] == "infoflow":
                    return _pending_delivery_receipt(payload)
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        class MalformedTransport:
            def wait(self, _request_id, _timeout_seconds):
                return {"request_id": "infoflow-request", "status": "APPROVE"}

        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        current_time = [now]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            gateway = PendingApprovalGateway(
                clock=lambda: current_time[0], state_store=orchestrator.state
            )
            infoflow = InfoflowApprovalClient(gateway)
            policy = {
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            }
            deadline = now + timedelta(seconds=1)
            requested = orchestrator.request_infoflow_approval(
                "run-1",
                "G0",
                "hash",
                deadline_at=deadline.isoformat(),
                member_policy=policy,
                comate_client=ComateTransport(),
                infoflow_client=infoflow,
            )
            approval_id = requested["approval_id"]
            current_time[0] = deadline
            with patch("approval_ledger.datetime") as ledger_datetime:
                ledger_datetime.now.return_value = deadline
                ledger_datetime.fromisoformat.side_effect = datetime.fromisoformat
                timed_out = orchestrator.wait_infoflow_approval(
                    "run-1", approval_id, "hash", infoflow, 0
                )
                repeated = orchestrator.wait_infoflow_approval(
                    "run-1", approval_id, "hash", infoflow, 0
                )
            handoffs = orchestrator.state.incomplete_handoffs("run-1")

            malformed_orchestrator = Orchestrator(Path(directory) / "malformed")
            strict_intake(malformed_orchestrator.state)
            malformed_requested = malformed_orchestrator.request_infoflow_approval(
                "run-1",
                "G0",
                "hash",
                deadline_at=(now + timedelta(hours=1)).isoformat(),
                member_policy=policy,
                comate_client=ComateTransport(),
                infoflow_client=ComateTransport(),
            )
            malformed_id = malformed_requested["approval_id"]
            malformed = malformed_orchestrator.wait_infoflow_approval(
                "run-1",
                malformed_id,
                "hash",
                InfoflowApprovalClient(MalformedTransport()),
                0,
            )
            malformed_audit = malformed_orchestrator.approvals.responses(malformed_id)

        self.assertEqual(timed_out["reason_code"], "APPROVAL_TIMEOUT")
        self.assertEqual(repeated["reason_code"], "APPROVAL_TIMEOUT")
        self.assertEqual(timed_out["handoff"], repeated["handoff"])
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(malformed["reason_code"], "APPROVAL_ENVELOPE_INVALID")
        self.assertEqual(
            [entry["rejected_reason"] for entry in malformed_audit],
            ["APPROVAL_ENVELOPE_INVALID"],
        )

    def test_orchestrator_rejects_a_typed_gateway_record_with_changed_bound_policy(self):
        class DeliveryTransport:
            def request(self, payload):
                if payload["channel"] == "infoflow":
                    return _pending_delivery_receipt(payload)
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        class ChangedPolicyTransport:
            def wait(self, request_id, _timeout_seconds):
                return {
                    "request_id": request_id,
                    "approval_id": approval_id,
                    "run_id": "run-1",
                    "channel": "infoflow",
                    "input_hash": "hash",
                    "member_policy": {
                        "comate": ["owner@example.test"],
                        "infoflow": ["attacker@example.test"],
                    },
                    "status": "PENDING",
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "deadline_at": deadline,
                    "heartbeat_at": None,
                    "updated_at": None,
                    "reply": None,
                    "handoff": None,
                    "reason_code": None,
                }

        deadline = "2026-08-11T10:00:00+00:00"
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            policy = {
                "comate": ["owner@example.test"],
                "infoflow": ["owner@example.test"],
            }
            requested = orchestrator.request_infoflow_approval(
                "run-1",
                "G0",
                "hash",
                deadline_at=deadline,
                member_policy=policy,
                comate_client=DeliveryTransport(),
                infoflow_client=DeliveryTransport(),
            )
            approval_id = requested["approval_id"]
            result = orchestrator.wait_infoflow_approval(
                "run-1",
                approval_id,
                "hash",
                InfoflowApprovalClient(ChangedPolicyTransport()),
                0,
            )
            audit = orchestrator.approvals.responses(approval_id)

        self.assertEqual(result["reason_code"], "APPROVAL_ENVELOPE_INVALID")
        self.assertEqual(
            [entry["rejected_reason"] for entry in audit],
            ["APPROVAL_ENVELOPE_INVALID"],
        )

    def test_default_strict_deadline_is_exactly_ten_hours_in_ledger_and_gateway(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        expected_deadline = "2026-08-11T10:00:00+00:00"
        with tempfile.TemporaryDirectory() as directory:
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            with patch("approval_ledger.datetime") as ledger_datetime:
                ledger_datetime.now.return_value = now
                ledger_datetime.fromisoformat.side_effect = datetime.fromisoformat
                approval = ledger.request(
                    "G0",
                    "hash",
                    ["comate", "infoflow"],
                    run_id="run-1",
                    member_policy={
                        "comate": ["owner@example.test"],
                        "infoflow": ["owner@example.test"],
                    },
                )
            gateway = PendingApprovalGateway(
                clock=lambda: now,
                state_store=StateStore(Path(directory) / "state.sqlite"),
            )
            gateway_record = gateway.request(
                {
                    "request_id": "approval-1",
                    "run_id": "run-1",
                    "approval_id": approval["approval_id"],
                    "channel": "infoflow",
                    "input_hash": "hash",
                    "deadline_at": approval["deadline_at"],
                    "member_policy": approval["member_policy"],
                }
            )

        self.assertEqual(approval["deadline_at"], expected_deadline)
        self.assertEqual(gateway_record["deadline_at"], expected_deadline)

    def test_gateway_rejects_unsupported_channels_and_freezes_member_policy(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            gateway = PendingApprovalGateway(clock=lambda: now, state_store=StateStore(Path(directory) / "state.sqlite"))
            for channel, policy in (
                ("codex", {"comate": ["owner@example.test"], "codex": ["owner@example.test"]}),
                ("infoflow", {"comate": ["owner@example.test"], "infoflow": ["owner"]}),
                ("infoflow", {"comate": ["owner@example.test"], "infoflow": [7]}),
            ):
                with self.assertRaisesRegex(ValueError, "APPROVAL_ENVELOPE_INVALID"):
                    gateway.request({
                        "request_id": channel, "run_id": "run-1", "approval_id": "approval-1",
                        "channel": channel, "input_hash": "hash", "deadline_at": (now + timedelta(minutes=1)).isoformat(),
                        "member_policy": policy,
                    })
            policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
            created = gateway.request({
                "request_id": "strict", "run_id": "run-1", "approval_id": "approval-1",
                "channel": "infoflow", "input_hash": "hash", "deadline_at": (now + timedelta(minutes=1)).isoformat(),
                "member_policy": policy,
            })
            with self.assertRaisesRegex(ValueError, "APPROVAL_REQUEST_CONFLICT"):
                gateway.request({
                    "request_id": "strict", "run_id": "run-1", "approval_id": "approval-1",
                    "channel": "infoflow", "input_hash": "hash", "deadline_at": (now + timedelta(minutes=2)).isoformat(),
                    "member_policy": policy,
                })
            policy["infoflow"].append("attacker@example.test")
            created["member_policy"]["infoflow"].append("also-attacker@example.test")
            rejected = gateway.reply("strict", {
                "run_id": "run-1", "approval_id": "approval-1", "channel": "infoflow",
                "input_hash": "hash", "decision": "APPROVE", "responder": "attacker@example.test",
            })

        self.assertEqual(rejected["reason_code"], "APPROVAL_RESPONDER_UNAUTHORIZED")

    def test_gateway_and_ledger_share_deadline_and_all_timeout_paths_persist_one_handoff(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        deadline = (now + timedelta(seconds=1)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            approval = ledger.request(
                "G0", "hash", ["comate", "infoflow"], run_id="run-1", deadline_at=deadline,
                member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
            )
            gateway = PendingApprovalGateway(clock=lambda: now, state_store=state)
            created = gateway.request({
                "request_id": "deadline", "run_id": "run-1", "approval_id": approval["approval_id"],
                "channel": "infoflow", "input_hash": "hash", "deadline_at": approval["deadline_at"],
                "member_policy": approval["member_policy"],
            })
            waited = gateway.wait("deadline", 0, now=now + timedelta(seconds=1))
            replied = gateway.reply("deadline", {"run_id": "run-1", "approval_id": approval["approval_id"], "channel": "infoflow", "input_hash": "hash", "decision": "APPROVE", "responder": "owner@example.test"})
            explicit = gateway.timeout("deadline", now=now + timedelta(seconds=2))
            handoffs = state.incomplete_handoffs("run-1")

        self.assertEqual(created["deadline_at"], approval["deadline_at"])
        self.assertEqual(waited["status"], "TIMEOUT")
        self.assertEqual(replied["status"], "TIMEOUT")
        self.assertEqual(explicit["reason_code"], "APPROVAL_ALREADY_RESOLVED")
        self.assertEqual(len(handoffs), 1)

    def test_orchestrator_audits_missing_and_malformed_wait_replies(self):
        class Transport:
            def request(self, payload):
                if payload["channel"] == "infoflow":
                    return _pending_delivery_receipt(payload)
                return {"request_id": "comate-request"}
            def reconcile(self, _payload): return None
            def wait(self, _request_id, _timeout):
                return {"run_id": "run-1", "approval_id": approval_id, "channel": "infoflow", "input_hash": "hash", "decision": "MAYBE", "responder": "owner@example.test"}

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
            requested = orchestrator.request_infoflow_approval("run-1", "G0", "hash", deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), member_policy=policy, comate_client=Transport(), infoflow_client=Transport())
            approval_id = requested["approval_id"]
            missing = orchestrator.receive_infoflow_reply("run-1", approval_id, "hash", {"run_id": "run-1", "approval_id": approval_id, "channel": "infoflow", "input_hash": "hash", "responder": "owner@example.test"})
            invalid_decision = orchestrator.receive_infoflow_reply(
                "run-1",
                approval_id,
                "hash",
                {
                    "run_id": "run-1",
                    "approval_id": approval_id,
                    "channel": "infoflow",
                    "input_hash": "hash",
                    "decision": "MAYBE",
                    "responder": "owner@example.test",
                },
            )
            malformed_wait = orchestrator.wait_infoflow_approval("run-1", approval_id, "hash", Transport(), 0)
            audited = orchestrator.approvals.responses(approval_id)

        self.assertEqual(missing["reason_code"], "APPROVAL_ENVELOPE_INVALID")
        self.assertEqual(invalid_decision["reason_code"], "APPROVAL_DECISION_INVALID")
        self.assertEqual(malformed_wait["reason_code"], "APPROVAL_ENVELOPE_INVALID")
        self.assertEqual(
            [row["rejected_reason"] for row in audited],
            [
                "APPROVAL_ENVELOPE_INVALID",
                "APPROVAL_DECISION_INVALID",
                "APPROVAL_ENVELOPE_INVALID",
            ],
        )

    def test_gateway_validates_run_bound_approval_envelopes_and_timeout_handoff(self):
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            gateway = PendingApprovalGateway(clock=lambda: now, state_store=state)
            with self.assertRaisesRegex(ValueError, "APPROVAL_ENVELOPE_INVALID"):
                gateway.request({"request_id": "missing-envelope", "input_hash": "hash"})
            created = gateway.request({
                "request_id": "approval-1", "run_id": "run-1", "approval_id": "approval-1",
                "channel": "infoflow", "input_hash": "hash", "deadline_at": (now + timedelta(seconds=1)).isoformat(),
                "member_policy": {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]},
            })
            unauthorized = gateway.reply("approval-1", {
                "run_id": "run-1", "approval_id": "approval-1", "channel": "infoflow",
                "input_hash": "hash", "decision": "APPROVE", "responder": "other@example.test",
            })
            timed_out = gateway.timeout("approval-1", now=now + timedelta(seconds=2))
            repeated = gateway.timeout("approval-1", now=now + timedelta(seconds=3))
            handoffs = state.incomplete_handoffs("run-1")

        self.assertEqual(created["run_id"], "run-1")
        self.assertEqual(unauthorized["reason_code"], "APPROVAL_RESPONDER_UNAUTHORIZED")
        self.assertEqual(timed_out["status"], "TIMEOUT")
        self.assertEqual(repeated["reason_code"], "APPROVAL_ALREADY_RESOLVED")
        self.assertEqual(len(handoffs), 1)

    def test_orchestrator_wait_reply_heartbeat_and_timeout_are_run_bound(self):
        class Transport:
            def __init__(self, channel): self.channel = channel
            def request(self, payload):
                if self.channel == "infoflow":
                    return _pending_delivery_receipt(
                        payload, request_id="infoflow-request"
                    )
                return {"request_id": "comate-request"}
            def reconcile(self, _payload): return None
            def wait(self, request_id, _timeout):
                approval = orchestrator.approvals.get(approval_id)
                return {
                    "request_id": request_id,
                    "run_id": "run-1",
                    "approval_id": approval_id,
                    "channel": "infoflow",
                    "input_hash": "hash",
                    "member_policy": approval["member_policy"],
                    "status": "APPROVE",
                    "created_at": "2026-08-11T00:00:00+00:00",
                    "deadline_at": approval["deadline_at"],
                    "heartbeat_at": None,
                    "updated_at": "2026-08-11T00:01:00+00:00",
                    "reply": {
                        "reply_id": "reply-1",
                        "decision": "APPROVE",
                        "responder": "owner@example.test",
                        "received_at": "2026-08-11T00:01:00+00:00",
                    },
                    "handoff": None,
                    "reason_code": None,
                }

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
            result = orchestrator.request_infoflow_approval("run-1", "G0", "hash", member_policy=policy, comate_client=Transport("comate"), infoflow_client=Transport("infoflow"))
            approval_id = result["approval_id"]
            waited = orchestrator.wait_infoflow_approval("run-1", approval_id, "hash", Transport("infoflow"), 0)
            heartbeat = orchestrator.heartbeat_infoflow_approval("run-1", approval_id, datetime.now(timezone.utc).isoformat())
            timeout = orchestrator.timeout_infoflow_approval("run-1", approval_id, "hash")

        self.assertEqual(waited["effective_decision"], "APPROVE")
        self.assertEqual(heartbeat["run_id"], "run-1")
        self.assertEqual(timeout["reason_code"], "APPROVAL_ALREADY_RESOLVED")

    def test_orchestrator_delivers_identical_run_bound_approval_to_both_channels_with_intents(self):
        class ApprovalTransport:
            def __init__(self, channel):
                self.channel = channel
                self.calls = []
            def request(self, payload):
                self.calls.append(payload)
                if self.channel == "infoflow":
                    return _pending_delivery_receipt(
                        payload, request_id="infoflow-request"
                    )
                return {"request_id": f"{self.channel}-request", "receipt": self.channel}
            def reconcile(self, payload):
                return None

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            orchestrator.state.transition("run-1", "INTAKE", {"project": "bgw", "requirement_id": "BGW-1", "profile_hash": "profile-a"})
            policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
            comate = ApprovalTransport("comate")
            infoflow = ApprovalTransport("infoflow")
            result = orchestrator.request_infoflow_approval(
                "run-1", "G0", "hash-a", member_policy=policy,
                evidence={"icafe": "BGW-1"}, comate_client=comate, infoflow_client=infoflow,
            )
            repeated = orchestrator.request_infoflow_approval(
                "run-1", "G0", "hash-a", member_policy=policy,
                evidence={"icafe": "BGW-1"}, comate_client=comate, infoflow_client=infoflow,
            )
            changed = orchestrator.request_infoflow_approval(
                "run-1", "G0", "hash-a", member_policy=policy,
                evidence={"icafe": "changed"}, comate_client=comate, infoflow_client=infoflow,
            )
            pending = orchestrator.state.pending_intents("run-1")

        self.assertEqual(result["approval_id"], repeated["approval_id"])
        self.assertEqual(len(comate.calls), 1)
        self.assertEqual(len(infoflow.calls), 1)
        self.assertEqual(comate.calls[0]["approval"], infoflow.calls[0]["approval"])
        self.assertEqual({comate.calls[0]["channel"], infoflow.calls[0]["channel"]}, {"comate", "infoflow"})
        self.assertEqual(len(pending), 0)
        self.assertEqual({item["channel"] for item in result["delivery_receipts"]}, {"comate", "infoflow"})
        self.assertEqual(changed["reason_code"], "APPROVAL_DELIVERY_CONFLICT")

    def test_group_client_writes_through_the_bot_gateway(self):
        class Bot:
            def __init__(self): self.calls = []
            def create_group(self, request):
                self.calls.append(("create", request))
                return {"group_id": "8", "bot_id": "bot-1"}
            def send_group_markdown(self, group_id, content, at_users):
                self.calls.append(("message", group_id, content, at_users))
                return {"message_id": "9", "group_id": group_id}
        bot = Bot()
        client = InfoflowGroupClient(bot_client=bot)
        group = client.create_or_reuse({"group_name": "n", "owner": "owner@example.test", "member_snapshot": ["owner@example.test"], "friendlyLevel": 3})
        message = client.send_markdown("8", "@owner@example.test body", ["owner@example.test"], "k")
        self.assertEqual(group, {"group_id": "8", "group_name": "n", "bot_id": "bot-1"})
        self.assertEqual(message["message_id"], "9")
        self.assertEqual(bot.calls[0][1]["friendly_level"], 3)
        self.assertEqual(bot.calls[1][3], ["owner@example.test"])

    def test_group_client_rejects_a_group_receipt_without_its_bot(self):
        class Bot:
            def create_group(self, _request):
                return {"group_id": "41"}

        client = InfoflowGroupClient(bot_client=Bot())
        with self.assertRaisesRegex(ValueError, "BOT_AGENT_ID_UNAVAILABLE"):
            client.create_or_reuse({
                "group_name": "BGW-19-Login-flow",
                "owner": "owner@example.test",
                "member_snapshot": ["owner@example.test"],
                "friendlyLevel": 3,
            })
        with self.assertRaisesRegex(ValueError, "MESSAGE_MENTION_MISMATCH"):
            client.send_markdown("8", "body", ["owner@example.test"], "k")


class ApprovalDeliveryFailureTests(unittest.TestCase):
    """What the control plane does when a channel will not take the card.

    Aborting the loop on the first failure used to hide the request from *both*
    audiences, and every failure was reported as an unknown outcome, which parks the
    run. Neither is right: a client that raised before dispatching proves nothing was
    sent.
    """

    policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}

    def _infoflow(self):
        class Infoflow:
            def __init__(self):
                self.calls = []

            def request(self, payload):
                self.calls.append(payload)
                return _pending_delivery_receipt(payload)

            def reconcile(self, _payload):
                return None

        return Infoflow()

    def _request(self, orchestrator, comate, infoflow, action="G0"):
        return orchestrator.request_infoflow_approval(
            "run-1", action, "hash-a", member_policy=self.policy,
            evidence={"icafe": "BGW-1"}, comate_client=comate, infoflow_client=infoflow,
        )

    def test_a_client_defect_is_retryable_and_leaves_no_claim(self):
        class BrokenComate:
            """A wiring defect: the transport this client needs is not there."""

            def request(self, _payload):
                raise AttributeError("'NoneType' object has no attribute 'post'")

            def reconcile(self, _payload):
                return None

        class WorkingComate:
            def __init__(self):
                self.calls = []

            def request(self, payload):
                self.calls.append(payload)
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            infoflow = self._infoflow()
            failed = self._request(orchestrator, BrokenComate(), infoflow)
            approval = orchestrator.approvals.get(failed["approval_id"])

            self.assertEqual(failed["reason_code"], "APPROVAL_DELIVERY_FAILED")
            self.assertTrue(failed["retry_allowed"])
            self.assertIn("AttributeError", failed["detail"])
            # Nothing was sent, so nothing is owed a reconciliation. A claim left here
            # parks the run in RECOVERY_REQUIRED over a write nobody performed.
            self.assertEqual(orchestrator.state.pending_intents("run-1"), [])
            self.assertEqual(approval["status"], "DELIVERY_FAILED")
            self.assertEqual(approval["delivery_failure"]["channels"], ["comate"])
            self.assertEqual(approval["delivery_failure"]["error_types"], ["AttributeError"])
            # The dead channel no longer hides the request from the live one.
            self.assertEqual(len(infoflow.calls), 1)
            self.assertEqual(
                [entry["channel"] for entry in approval["delivery_receipts"]], ["infoflow"]
            )

            comate = WorkingComate()
            repaired = self._request(orchestrator, comate, infoflow)

            self.assertEqual(repaired["approval_id"], failed["approval_id"])
            self.assertEqual(repaired["status"], "PENDING")
            self.assertIsNone(repaired["delivery_failed_at"])
            self.assertEqual(
                {entry["channel"] for entry in repaired["delivery_receipts"]},
                {"comate", "infoflow"},
            )
            # The channel that already delivered is not asked twice.
            self.assertEqual(len(infoflow.calls), 1)
            self.assertEqual(len(comate.calls), 1)

    def test_a_transport_error_is_still_an_unknown_outcome(self):
        """The split is by exception class, not by whichever answer is convenient.

        A socket that died mid-request may well have delivered the card, so the claim
        stays open and the run has to go and ask.
        """

        class DeadSocketComate:
            def request(self, _payload):
                raise OSError("connection reset by peer")

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            infoflow = self._infoflow()
            result = self._request(orchestrator, DeadSocketComate(), infoflow)
            approval = orchestrator.approvals.get(result["approval_id"])

            self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
            self.assertFalse(result["retry_allowed"])
            self.assertEqual(
                [item["operation"] for item in orchestrator.state.pending_intents("run-1")],
                ["approval.delivery.comate"],
            )
            self.assertEqual(approval["status"], "DELIVERY_FAILED")
            self.assertFalse(approval["delivery_failure"]["retry_allowed"])
            self.assertEqual(len(infoflow.calls), 1)

    def test_reissue_frees_an_undeliverable_gate_but_not_a_retryable_one(self):
        class DeadSocketComate:
            def request(self, _payload):
                raise OSError("connection reset by peer")

        class BrokenComate:
            def request(self, _payload):
                raise AttributeError("no transport")

            def reconcile(self, _payload):
                return None

        class WorkingComate:
            def request(self, _payload):
                return {"request_id": "comate-request"}

            def reconcile(self, _payload):
                return None

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            retryable = self._request(orchestrator, BrokenComate(), self._infoflow())
            refused = orchestrator.reissue_infoflow_approval(
                "run-1", "G0", "hash-a", member_policy=self.policy,
                infoflow_client=self._infoflow(), comate_client=BrokenComate(),
            )
            # Fixing the defect and asking again re-posts only the dead channel, so
            # burning a new action name on it would lose that retry for nothing.
            self.assertEqual(refused["reason_code"], "APPROVAL_DELIVERY_RETRYABLE")
            self.assertEqual(refused["approval_id"], retryable["approval_id"])

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            stuck = self._request(orchestrator, DeadSocketComate(), self._infoflow())
            reissued = orchestrator.reissue_infoflow_approval(
                "run-1", "G0", "hash-a", member_policy=self.policy,
                infoflow_client=self._infoflow(), comate_client=WorkingComate(),
            )

            self.assertNotEqual(reissued["approval_id"], stuck["approval_id"])
            self.assertEqual(reissued["action"], "G0#retry-1")
            self.assertEqual(reissued["input_hash"], "hash-a")
            self.assertEqual(reissued["status"], "PENDING")
            self.assertEqual(
                {entry["channel"] for entry in reissued["delivery_receipts"]},
                {"comate", "infoflow"},
            )
            # The attempt nobody could answer stays in the audit trail.
            self.assertEqual(
                sorted(
                    approval["status"] for approval in orchestrator.approvals.for_run("run-1")
                ),
                ["DELIVERY_FAILED", "PENDING"],
            )

    def test_an_undeliverable_gate_is_not_offered_to_the_reply_watcher(self):
        class BrokenComate:
            def request(self, _payload):
                raise AttributeError("no transport")

            def reconcile(self, _payload):
                return None

        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            strict_intake(orchestrator.state)
            self._request(orchestrator, BrokenComate(), self._infoflow())
            # No channel is showing that card, so polling for a reply on it only
            # teaches the watcher to report silence as though somebody were deciding.
            self.assertEqual(orchestrator.approvals.pending(), [])
