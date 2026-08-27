import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_channels import ApprovalChannels
from clients.icafe_client import CafeClient
from clients.icode_client import IcodeClient
from clients.ipipe_client import IpipeClient
from review_provider import ReviewProvider
from state_store import StateStore

try:
    from cli_transport import CliTransport, CliTransportError
except ModuleNotFoundError as missing_adapter:
    CliTransport = None
    CliTransportError = Exception

try:
    from clients.ku_client import KuClient
except ModuleNotFoundError:
    KuClient = None


class FakeTransport:
    skip_preflight = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, **options):
        self.calls.append((list(argv), dict(options)))
        if not self.responses:
            raise AssertionError(f"unexpected transport call: {argv!r}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def transport_error(reason_code="CLI_TIMEOUT"):
    if CliTransport is None:
        return TimeoutError(reason_code)
    return CliTransportError(reason_code, {"argv": ["icafe-cli", "comment", "create"]})


def card_payload(card_id="cloud-iCafe-22229", *, status="New"):
    space, sequence = card_id.rsplit("-", 1)
    return {
        "code": 200,
        "cards": [
            {
                "sequence": int(sequence),
                "spacePrefixCode": space,
                "title": "Requirement",
                "detail": "<p>Behavior</p>",
                "status": status,
                "type": {"name": "Story"},
                "responsiblePeople": [
                    {"username": "dev", "name": "Developer", "email": "dev@example.test"}
                ],
                "createdUser": {"username": "owner"},
                "createdTime": "2026-08-01 10:00:00",
                "lastModifiedUser": {"username": "dev"},
                "lastModifiedTime": "2026-08-02 11:00:00",
                "properties": [
                    {"propertyName": "Acceptance", "displayValue": "AC-1"},
                    {"propertyName": "Priority", "displayValue": "P1"},
                ],
                "attachments": [{"id": "att-1", "url": "https://files.example.test/a"}],
                "links": ["https://example.test/spec"],
            }
        ],
    }


def ku_url(doc_id, repo_id="repo-1"):
    return f"https://ku.baidu-int.com/knowledge/space/category/{repo_id}/{doc_id}"


CLOSE_KU_URL = (
    "https://ku.baidu-int.com/knowledge/"
    "HFVrC7hq1Q/2tsPs8CtSd/E3d4LRExEl/1xosIYvQX3qxeI"
)


def ku_content(doc_id, text, repo_id="repo-1"):
    return {
        "returnCode": 200,
        "success": True,
        "result": {
            "text": text,
            "docGuid": doc_id,
            "repositoryGuid": repo_id,
            "url": ku_url(doc_id, repo_id),
        },
    }


def ku_version(doc_id, version, init_type):
    return {
        "returnCode": 200,
        "success": True,
        "result": {
            "data": [{"docGuid": doc_id, "versionId": version, "initType": init_type}]
        },
    }


def ku_repo(*documents):
    return {
        "returnCode": 200,
        "success": True,
        "result": {"count": len(documents), "total": len(documents), "data": list(documents)},
    }


def ku_repo_document(doc_id, title, repo_id="repo-1"):
    return {
        "docGuid": doc_id,
        "name": title,
        "repositoryGuid": repo_id,
        "url": ku_url(doc_id, repo_id),
    }


class CafeClientTests(unittest.TestCase):
    def test_snapshot_hash_is_stable(self):
        client = CafeClient(
            lambda card_id: {
                "card_id": card_id,
                "title": "Requirement",
                "body": "Behavior",
                "acceptance_criteria": ["AC-1"],
                "attachments": ["att-1"],
            }
        )
        first = client.snapshot("BGW-1")
        second = client.snapshot("BGW-1")

        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(len(first["content_hash"]), 64)

    def test_explicit_card_uses_final_hyphen_and_builds_complete_snapshot(self):
        transport = FakeTransport([card_payload()])
        client = CafeClient(transport=transport, preflight=False)

        snapshot = client.snapshot("cloud-iCafe-22229")

        self.assertEqual(
            transport.calls[0][0],
            [
                "icafe-cli",
                "card",
                "get",
                "--space",
                "cloud-iCafe",
                "--sequence",
                "22229",
                "--brief",
            ],
        )
        self.assertEqual(snapshot["canonical_card_id"], "cloud-iCafe-22229")
        self.assertEqual(snapshot["body"], "<p>Behavior</p>")
        self.assertEqual(snapshot["acceptance"], ["AC-1"])
        self.assertEqual(snapshot["fields"]["Priority"], "P1")
        self.assertEqual(snapshot["status"], "New")
        self.assertEqual(snapshot["type"], "Story")
        self.assertEqual(len(snapshot["content_hash"]), 64)

    def test_invalid_card_id_fails_without_guessing_or_calling_transport(self):
        transport = FakeTransport([])
        result = CafeClient(transport=transport, preflight=False).snapshot("BGW-not-numeric")

        self.assertEqual(result["reason_code"], "ICAFE_CARD_ID_INVALID")
        self.assertEqual(transport.calls, [])

    def test_snapshot_rejects_mismatched_remote_identity_and_malformed_required_shapes(self):
        wrong = card_payload("OTHER-9")
        malformed = card_payload("BGW-1")
        malformed["cards"][0]["responsiblePeople"] = "not-a-list"
        transport = FakeTransport([wrong, malformed])
        client = CafeClient(transport=transport, preflight=False)

        mismatch = client.snapshot("BGW-1")
        invalid = client.snapshot("BGW-1")

        self.assertEqual(mismatch["reason_code"], "ICAFE_SNAPSHOT_IDENTITY_MISMATCH")
        self.assertEqual(invalid["reason_code"], "ICAFE_SNAPSHOT_INVALID")

    def test_missing_explicit_card_uses_smart_find_only_when_candidate_is_unique(self):
        transport = FakeTransport(
            [
                {
                    "status": 200,
                    "cards": [
                        {"spacePrefixCode": "cloud-iCafe", "sequence": 22229, "title": "Requirement"}
                    ],
                },
                card_payload(),
            ]
        )
        snapshot = CafeClient(transport=transport, preflight=False).snapshot("")

        self.assertEqual(transport.calls[0][0], ["icafe-cli", "card", "smart-find"])
        self.assertEqual(snapshot["canonical_card_id"], "cloud-iCafe-22229")

    def test_first_session_runs_version_and_login_preflight_once(self):
        transport = FakeTransport([{"ok": True}, {"ok": True}, card_payload(), card_payload()])
        transport.skip_preflight = False
        client = CafeClient(transport=transport)

        client.snapshot("BGW-1")
        client.snapshot("BGW-1")

        self.assertEqual(transport.calls[0][0], ["icafe-cli", "version"])
        self.assertEqual(transport.calls[1][0], ["icafe-cli", "login", "status"])
        self.assertEqual(len(transport.calls), 4)

    def test_card_business_code_failure_is_mapped(self):
        if CliTransport is None:
            self.fail("CliTransport is not implemented")
        runner = FakeRunner(stdout='{"code":404,"message":"missing"}')
        transport = CliTransport(runner=runner)
        client = CafeClient(transport=transport, preflight=False)

        result = client.snapshot("BGW-1")

        self.assertEqual(result["reason_code"], "OBJECT_NOT_FOUND")
        self.assertNotIn("message", result)

    def test_comment_queries_before_create_and_duplicate_returns_existing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            marker = CafeClient.comment_marker("phase-1")
            transport = FakeTransport(
                [
                    {"status": 200, "result": []},
                    {"status": 200, "message": "OK"},
                    {"status": 200, "result": [{"id": 91, "content": f"done\n{marker}"}]},
                    {"status": 200, "result": [{"id": 91, "content": f"done\n{marker}"}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-1",
                preflight=False,
            )

            first = client.comment("BGW-1", "done", "phase-1")
            repeated = client.comment("BGW-1", "done", "phase-1")

        self.assertTrue(first["ok"])
        self.assertEqual(repeated["comment_id"], 91)
        creates = [call for call, _ in transport.calls if call[1:3] == ["comment", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn(marker, creates[0][creates[0].index("--content") + 1])
        self.assertEqual(first["evidence_refs"], ["icafe:BGW-1/91"])

    def test_completed_comment_receipt_replays_without_remote_query(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            marker = CafeClient.comment_marker("phase-replay")
            transport = FakeTransport(
                [
                    {"status": 200, "result": []},
                    {"status": 200, "message": "OK"},
                    {"status": 200, "result": [{"id": 93, "content": f"done\n{marker}"}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-comment-replay",
                preflight=False,
            )

            first = client.comment("BGW-1", "done", "phase-replay")
            repeated = client.comment("BGW-1", "done", "phase-replay")

        self.assertEqual(repeated, first)
        self.assertEqual(len(transport.calls), 3)

    def test_comment_marker_reuse_with_changed_content_is_a_conflict(self):
        marker = CafeClient.comment_marker("phase-conflict")
        with tempfile.TemporaryDirectory() as directory:
            client = CafeClient(
                transport=FakeTransport(
                    [{"status": 200, "result": [{"id": 94, "content": f"old content\n{marker}"}]}]
                ),
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-comment-conflict",
                preflight=False,
            )

            result = client.comment("BGW-1", "new content", "phase-conflict")

        self.assertEqual(result["reason_code"], "ICAFE_COMMENT_CONFLICT")

    def test_unknown_comment_result_leaves_pending_intent_and_retry_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            transport = FakeTransport(
                [
                    {"status": 200, "result": []},
                    transport_error(),
                    {"status": 200, "result": []},
                    {"status": 200, "result": []},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-unknown",
                preflight=False,
            )

            first = client.comment("BGW-2", "content that must not persist", "comment-key")
            repeated = client.comment("BGW-2", "content that must not persist", "comment-key")

            pending = state.pending_intents("run-unknown")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(repeated["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(len(pending), 1)
        self.assertNotIn("content that must not persist", json.dumps(pending))
        creates = [call for call, _ in transport.calls if call[1:3] == ["comment", "create"]]
        self.assertEqual(len(creates), 1)

    def test_status_update_checks_current_reachability_and_requeries_after_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "In progress"}]},
                    {"code": 200},
                    {"code": 200, "cards": card_payload(status="In progress")["cards"]},
                    {"status": 200, "statusName": "In progress"},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-status",
                preflight=False,
            )

            result = client.update_status("BGW-3", "In progress", "New")

        self.assertTrue(result["ok"])
        update_argv = transport.calls[2][0]
        self.assertEqual(
            update_argv,
            [
                "icafe-cli",
                "card",
                "update",
                "--space",
                "BGW",
                "--sequence",
                "3",
                "--status",
                "In progress",
            ],
        )
        self.assertNotIn("--no-check-status", update_argv)

    def test_completed_status_receipt_replays_without_remote_query_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"code": 200},
                    card_payload("BGW-3", status="Closed"),
                    {"status": 200, "statusName": "Closed"},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-status-replay",
                preflight=False,
            )

            first = client.update_status("BGW-3", "Closed", "New")
            repeated = client.update_status("BGW-3", "Closed", "New")

        self.assertEqual(repeated, first)
        self.assertEqual(len(transport.calls), 5)

    def test_definite_status_permission_failure_is_receipted_not_left_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    transport_error("PERMISSION_DENIED"),
                ]
            )
            result = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-status-denied",
                preflight=False,
            ).update_status("BGW-3", "Closed", "New")

            pending = state.pending_intents("run-status-denied")

        self.assertEqual(result["reason_code"], "PERMISSION_DENIED")
        self.assertEqual(pending, [])

    def test_unreachable_status_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport(
                [
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "In progress"}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-status",
                preflight=False,
            )

            result = client.update_status("BGW-4", "Closed", "New")

        self.assertEqual(result["reason_code"], "ICAFE_STATUS_UNREACHABLE")
        self.assertEqual(len(transport.calls), 2)

    def test_close_requires_ledger_approval_terminal_status_reason_and_ku_url(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = CafeClient(
                transport=FakeTransport([]),
                state_store=state,
                run_id="run-close",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: None,
                preflight=False,
            )

            result = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )

        self.assertEqual(result["reason_code"], "ICAFE_APPROVAL_REQUIRED")

    def test_close_rejects_noncanonical_ku_urls_before_approval_or_remote_calls(self):
        invalid_urls = [
            "http://ku.baidu-int.com/knowledge/space/category/repo-1/doc",
            "https://example.test/knowledge/space/category/repo-1/doc",
            "https://user@ku.baidu-int.com/knowledge/space/category/repo-1/doc",
            "https://ku.baidu-int.com:443/knowledge/space/category/repo-1/doc",
            "https://ku.baidu-int.com/knowledge/space/category/repo-1/doc?view=1",
            "https://ku.baidu-int.com/knowledge/space/category/repo-1/doc#section",
            "https://ku.baidu-int.com/knowledge/space/category/repo-1",
            "https://ku.baidu-int.com/knowledge/space/category/repo-1/doc/extra",
            "https://ku.baidu-int.com/knowledge/space//repo-1/doc",
            "https://ku.baidu-int.com/knowledge/space/category/repo%201/doc",
        ]
        for index, invalid_url in enumerate(invalid_urls):
            with self.subTest(url=invalid_url), tempfile.TemporaryDirectory() as directory:
                transport = FakeTransport([])
                client = CafeClient(
                    transport=transport,
                    state_store=StateStore(Path(directory) / "state.sqlite"),
                    run_id=f"run-close-url-{index}",
                    terminal_statuses=["Closed"],
                    approval_lookup=lambda _approval_id: (_ for _ in ()).throw(
                        AssertionError("invalid URL must fail before approval lookup")
                    ),
                    preflight=False,
                )

                result = client.close(
                    "BGW-5", "Closed", "superseded", invalid_url, {"approval_id": "a1"}
                )

            self.assertEqual(result["reason_code"], "ICAFE_KNOWLEDGE_URL_INVALID")
            self.assertEqual(transport.calls, [])

    def test_unreachable_close_never_posts_a_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            close_hash = CafeClient.close_input_hash(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL
            )
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "In progress"}]},
                ]
            )
            result = CafeClient(
                transport=transport,
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-close-unreachable",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            ).close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )

        self.assertEqual(result["reason_code"], "ICAFE_STATUS_UNREACHABLE")
        self.assertFalse(any(call[1:3] == ["comment", "create"] for call, _ in transport.calls))

    def test_close_rejects_approval_for_a_different_action_before_remote_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            client = CafeClient(
                transport=FakeTransport([]),
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-close-action",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "G5",
                    "input_hash": CafeClient.close_input_hash(
                        "BGW-5", "Closed", "superseded", CLOSE_KU_URL
                    ),
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            result = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )

        self.assertEqual(result["reason_code"], "ICAFE_APPROVAL_REQUIRED")

    def test_approved_close_replays_completed_result_without_remote_calls(self):
        close_hash = CafeClient.close_input_hash("BGW-5", "Closed", "superseded", CLOSE_KU_URL)
        marker = CafeClient.comment_marker(f"close:{close_hash}")
        expected_comment = (
            "Close/cancel reason: superseded\n"
            f"Knowledge: {CLOSE_KU_URL}\n"
            f"{marker}"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"code": 200},
                    card_payload("BGW-5", status="Closed"),
                    {"status": 200, "statusName": "Closed"},
                    {"status": 200, "result": []},
                    {"status": 200, "message": "OK"},
                    {"status": 200, "result": [{"id": 92, "content": expected_comment}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-close-success",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            result = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            call_count = len(transport.calls)
            repeated = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(repeated, result)
        self.assertEqual(len(transport.calls), call_count)
        all_arguments = [argument for call, _ in transport.calls for argument in call]
        self.assertNotIn("delete", all_arguments)
        create = next(call for call, _ in transport.calls if call[1:3] == ["comment", "create"])
        content = create[create.index("--content") + 1]
        self.assertIn("superseded", content)
        self.assertIn(CLOSE_KU_URL, content)

    def test_close_retry_reconciles_status_applied_and_unknown_comment(self):
        close_hash = CafeClient.close_input_hash("BGW-5", "Closed", "superseded", CLOSE_KU_URL)
        marker = CafeClient.comment_marker(f"close:{close_hash}")
        expected_comment = (
            "Close/cancel reason: superseded\n"
            f"Knowledge: {CLOSE_KU_URL}\n"
            f"{marker}"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"code": 200},
                    card_payload("BGW-5", status="Closed"),
                    {"status": 200, "statusName": "Closed"},
                    {"status": 200, "result": []},
                    transport_error("CLI_TIMEOUT"),
                    {"status": 200, "result": []},
                    card_payload("BGW-5", status="Closed"),
                    {"status": 200, "result": [{"id": 92, "content": expected_comment}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-close-partial",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            first = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            pending_after_first = state.pending_intents("run-close-partial")
            repeated = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            pending = state.pending_intents("run-close-partial")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        close_intent = next(
            item for item in pending_after_first if item["operation"] == "icafe.card.close"
        )
        self.assertEqual(close_intent["payload"]["expected_current"], "New")
        self.assertNotIn("superseded", json.dumps(close_intent))
        self.assertNotIn(CLOSE_KU_URL, json.dumps(close_intent))
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1:3] == ["card", "update"] for call in transport.calls), 1)
        self.assertEqual(sum(call[0][1:3] == ["comment", "create"] for call in transport.calls), 1)

    def test_pending_close_does_not_treat_completed_status_receipt_as_unknown(self):
        close_hash = CafeClient.close_input_hash("BGW-5", "Closed", "superseded", CLOSE_KU_URL)
        marker = CafeClient.comment_marker(f"close:{close_hash}")
        expected_comment = (
            "Close/cancel reason: superseded\n"
            f"Knowledge: {CLOSE_KU_URL}\n"
            f"{marker}"
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"code": 200},
                    card_payload("BGW-5", status="Closed"),
                    {"status": 200, "statusName": "Closed"},
                    {"status": 200, "result": []},
                    transport_error("CLI_TIMEOUT"),
                    {"status": 200, "result": []},
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"status": 200, "result": [{"id": 94, "content": expected_comment}]},
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-close-completed-status",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            first = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            repeated = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            pending = state.pending_intents("run-close-completed-status")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1:3] == ["card", "update"] for call in transport.calls), 1)
        self.assertEqual(sum(call[0][1:3] == ["comment", "create"] for call in transport.calls), 1)

    def test_pending_close_does_not_repeat_unknown_status_when_card_remains_original(self):
        close_hash = CafeClient.close_input_hash("BGW-5", "Closed", "superseded", CLOSE_KU_URL)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    transport_error("CLI_TIMEOUT"),
                    card_payload("BGW-5", status="New"),
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=state,
                run_id="run-close-original",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            first = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            repeated = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            pending = state.pending_intents("run-close-original")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(repeated["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(
            {item["operation"] for item in pending},
            {"icafe.card.close", "icafe.card.update-status"},
        )
        self.assertEqual(sum(call[0][1:3] == ["card", "update"] for call in transport.calls), 1)

    def test_pending_close_fails_closed_when_card_moves_to_a_third_status(self):
        close_hash = CafeClient.close_input_hash("BGW-5", "Closed", "superseded", CLOSE_KU_URL)
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport(
                [
                    card_payload("BGW-5", status="New"),
                    {"status": 200, "statusName": "New"},
                    {"status": 200, "result": [{"statusName": "Closed"}]},
                    {"code": 200},
                    card_payload("BGW-5", status="Closed"),
                    {"status": 200, "statusName": "Closed"},
                    {"status": 200, "result": []},
                    transport_error("CLI_TIMEOUT"),
                    {"status": 200, "result": []},
                    card_payload("BGW-5", status="In progress"),
                ]
            )
            client = CafeClient(
                transport=transport,
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-close-third-state",
                terminal_statuses=["Closed"],
                approval_lookup=lambda _approval_id: {
                    "action": "ICAFE_CLOSE",
                    "input_hash": close_hash,
                    "effective_decision": "APPROVE",
                },
                preflight=False,
            )

            first = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )
            repeated = client.close(
                "BGW-5", "Closed", "superseded", CLOSE_KU_URL, {"approval_id": "a1"}
            )

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(repeated["reason_code"], "ICAFE_STATUS_CHANGED")
        self.assertEqual(repeated["current_status"], "In progress")


class FakeRunner:
    def __init__(self, *, stdout="{}", stderr="", returncode=0, exception=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exception = exception
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.exception is not None:
            raise self.exception
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class CliTransportTests(unittest.TestCase):
    def test_timeout_has_stable_reason_and_redacted_diagnostics(self):
        if CliTransport is None:
            self.fail("CliTransport is not implemented")
        runner = FakeRunner(
            exception=subprocess.TimeoutExpired(
                ["tool", "--token", "sentinel", "--content", "private body"], 4
            )
        )
        transport = CliTransport(runner=runner, timeout_seconds=4)

        with self.assertRaises(CliTransportError) as raised:
            transport.run(
                ["tool", "--token", "sentinel", "--content", "private body"],
                business_field="status",
            )

        self.assertEqual(raised.exception.reason_code, "CLI_TIMEOUT")
        diagnostic = json.dumps(raised.exception.diagnostic)
        self.assertNotIn("sentinel", diagnostic)
        self.assertNotIn("private body", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)

    def test_invalid_json_and_bounded_stderr_do_not_expose_raw_error(self):
        if CliTransport is None:
            self.fail("CliTransport is not implemented")
        secret = "stderr-secret-sentinel"
        runner = FakeRunner(stdout="not-json", stderr=secret * 1000)
        transport = CliTransport(runner=runner, stderr_limit=64)

        with self.assertRaises(CliTransportError) as raised:
            transport.run(["tool", "query"], business_field="status")

        self.assertEqual(raised.exception.reason_code, "CLI_INVALID_JSON")
        self.assertNotIn(secret, json.dumps(raised.exception.diagnostic))
        self.assertEqual(raised.exception.diagnostic["stderr_bytes"], len((secret * 1000).encode()))

    def test_ku_username_is_exported_without_persisting_environment_values(self):
        if CliTransport is None:
            self.fail("CliTransport is not implemented")
        runner = FakeRunner(stdout='{"returnCode":200,"success":true}')
        transport = CliTransport(runner=runner, environment={"BAIDU_CC_USERNAME": "tester"})

        result = transport.run(["ku", "query-content"], business_field="returnCode")

        self.assertEqual(result["returnCode"], 200)
        self.assertEqual(runner.calls[0][1]["env"]["BAIDU_CC_USERNAME"], "tester")
        self.assertNotIn("environment", transport.last_diagnostic)

    def test_exit_one_distinguishes_auth_failed_json_from_cobra_input_failure(self):
        auth = CliTransport(
            runner=FakeRunner(returncode=1, stderr='{"error":"auth_failed","message":"expired"}')
        )
        cobra = CliTransport(
            runner=FakeRunner(returncode=1, stderr='{"error":"required flag missing"}')
        )

        with self.assertRaises(CliTransportError) as auth_error:
            auth.run(["icafe-cli", "card", "get"])
        with self.assertRaises(CliTransportError) as input_error:
            cobra.run(["icafe-cli", "card", "get"])

        self.assertEqual(auth_error.exception.reason_code, "AUTH_REQUIRED")
        self.assertEqual(input_error.exception.reason_code, "INVALID_INPUT")


class KuClientTests(unittest.TestCase):
    def test_create_requires_state_and_never_writes_without_it(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        transport = FakeTransport([])
        result = KuClient(transport=transport, repo_id="repo-1", username="tester").create_artifact(
            "root-1", "01-spec", "# Spec"
        )

        self.assertEqual(result["reason_code"], "PERSISTENCE_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_create_publish_and_each_write_are_verified_and_receipted(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        markdown = "# Spec\n\nBehavior"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_repo(),
                    {
                        "returnCode": 200,
                        "success": True,
                        "result": {
                            "docGuid": "child-1",
                            "repositoryGuid": "repo-1",
                            "url": ku_url("child-1"),
                            "title": "01-spec",
                        },
                    },
                    ku_content("child-1", KuClient.marked_markdown("root-1", "01-spec", markdown)),
                    ku_version("child-1", 3, 1),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "child-1"}},
                    ku_content("child-1", KuClient.marked_markdown("root-1", "01-spec", markdown)),
                    ku_version("child-1", 4, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-ku",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-1", "01-spec", markdown)
            pending = state.pending_intents("run-ku")

        self.assertTrue(result["ok"])
        self.assertEqual(result["doc_id"], "child-1")
        self.assertEqual(result["version"], "4")
        self.assertEqual(pending, [])
        self.assertEqual(transport.calls[1][0][1], "create-doc")
        self.assertEqual(transport.calls[4][0][1], "publish-doc")
        self.assertEqual(result["evidence_refs"], ["ku:child-1/4"])

    def test_unknown_create_reconciles_remote_child_by_parent_title_and_marker_without_recreate(self):
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_repo(),
                    transport_error("CLI_TIMEOUT"),
                    ku_repo(ku_repo_document("child-1", "01-spec")),
                    ku_content("child-1", marked),
                    ku_version("child-1", 3, 4),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "child-1"}},
                    ku_content("child-1", marked),
                    ku_version("child-1", 4, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-create-unknown",
                repo_id="repo-1",
                username="tester",
            )

            first = client.create_artifact("root-1", "01-spec", markdown)
            repeated = client.create_artifact("root-1", "01-spec", markdown)

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(
            sum(call[0][1] == "create-doc" for call in transport.calls),
            1,
        )
        repo_query = transport.calls[2][0]
        self.assertEqual(repo_query[1], "query-repo")
        self.assertEqual(repo_query[repo_query.index("--parent-doc-id") + 1], "root-1")

    def test_completed_create_receipt_resumes_unknown_publish_without_recreating_child(self):
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_repo(),
                    {
                        "returnCode": 200,
                        "success": True,
                        "result": {
                            "docGuid": "child-1",
                            "repositoryGuid": "repo-1",
                            "url": ku_url("child-1"),
                            "title": "01-spec",
                        },
                    },
                    ku_content("child-1", marked),
                    ku_version("child-1", 3, 4),
                    transport_error("CLI_TIMEOUT"),
                    ku_content("child-1", marked),
                    ku_version("child-1", 4, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-publish-unknown",
                repo_id="repo-1",
                username="tester",
            )

            first = client.create_artifact("root-1", "01-spec", markdown)
            repeated = client.create_artifact("root-1", "01-spec", markdown)
            pending = state.pending_intents("run-publish-unknown")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "create-doc" for call in transport.calls), 1)
        self.assertEqual(sum(call[0][1] == "publish-doc" for call in transport.calls), 1)

    def test_existing_exact_index_reconciles_unknown_edit_then_publishes(self):
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_content("root-1", "# Run index"),
                    ku_version("root-1", 7, 0),
                    transport_error("CLI_TIMEOUT"),
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 8, 4),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 9, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-edit-unknown",
                repo_id="repo-1",
                username="tester",
            )

            first = client.update_index("root-1", entry)
            repeated = client.update_index("root-1", entry)
            pending = state.pending_intents("run-edit-unknown")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "edit-content" for call in transport.calls), 1)

    def test_existing_exact_index_reconciles_unknown_publish_and_clears_lower_intent(self):
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
        text = f"# Run index\n\n{indexed}"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_content("root-1", "# Run index"),
                    ku_version("root-1", 7, 0),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                    ku_content("root-1", text),
                    ku_version("root-1", 8, 4),
                    transport_error("CLI_TIMEOUT"),
                    ku_content("root-1", text),
                    ku_version("root-1", 9, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-index-publish-unknown",
                repo_id="repo-1",
                username="tester",
            )

            first = client.update_index("root-1", entry)
            repeated = client.update_index("root-1", entry)
            pending = state.pending_intents("run-index-publish-unknown")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "publish-doc" for call in transport.calls), 1)

    def test_index_marker_without_exact_single_entry_is_a_conflict(self):
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        marker = KuClient.index_marker("root-1", "01-spec", "child-1", "a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            client = KuClient(
                transport=FakeTransport(
                    [
                        ku_content("root-1", f"# Run index\n{marker}"),
                        ku_version("root-1", 7, 0),
                    ]
                ),
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-index-corrupt",
                repo_id="repo-1",
                username="tester",
            )

            result = client.update_index("root-1", entry)

        self.assertEqual(result["reason_code"], "KU_INDEX_CONFLICT")

    def test_definite_ku_create_permission_failure_is_receipted_not_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = KuClient(
                transport=FakeTransport([ku_repo(), transport_error("PERMISSION_DENIED")]),
                state_store=state,
                run_id="run-create-denied",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-1", "01-spec", "# Spec")
            pending = state.pending_intents("run-create-denied")

        self.assertEqual(result["reason_code"], "PERMISSION_DENIED")
        self.assertEqual(pending, [])

    def test_create_verification_rejects_queried_repo_or_canonical_url_mismatch(self):
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_repo(),
                    {
                        "returnCode": 200,
                        "success": True,
                        "result": {
                            "docGuid": "child-1",
                            "repositoryGuid": "repo-1",
                            "url": ku_url("child-1"),
                            "title": "01-spec",
                        },
                    },
                    ku_content("child-1", marked, repo_id="other-repo"),
                    ku_version("child-1", 3, 4),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-query-identity",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-1", "01-spec", markdown)

        self.assertEqual(result["reason_code"], "KU_CREATE_VERIFICATION_FAILED")

    def test_existing_child_hash_mismatch_is_an_immutable_conflict(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        transport = FakeTransport(
            [
                ku_repo(ku_repo_document("child-1", "01-spec")),
                ku_content(
                    "child-1",
                    KuClient.marked_markdown("root-1", "01-spec", "old content"),
                ),
                ku_version("child-1", 2, 0),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = KuClient(
                transport=transport,
                state_store=StateStore(Path(directory) / "state.sqlite"),
                run_id="run-conflict",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-1", "01-spec", "new content")

        self.assertEqual(result["reason_code"], "KU_IMMUTABLE_CONFLICT")
        self.assertEqual(len(transport.calls), 3)

    def test_root_index_uses_exact_mdsl_append_then_publishes_and_verifies(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_content("root-1", "# Run index"),
                    ku_version("root-1", 7, 0),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 8, 4),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 9, 0),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-index",
                repo_id="repo-1",
                username="tester",
            )

            result = client.update_index("root-1", entry)

        self.assertTrue(result["ok"])
        edit = transport.calls[2][0]
        self.assertIn("--editor-mode", edit)
        self.assertEqual(edit[edit.index("--editor-mode") + 1], "mdsl")
        operation = json.loads(edit[edit.index("--operation") + 1])
        self.assertEqual(operation["mode"], "insert_after")
        self.assertEqual(operation["selectionWithEllipsis"], "# Run index")
        self.assertIn(KuClient.index_marker("root-1", "01-spec", "child-1", "a" * 64), operation["markdown"])

    def test_failed_ku_create_keeps_pending_intent(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            transport = FakeTransport(
                [
                    ku_repo(),
                    transport_error("CLI_TIMEOUT"),
                ]
            )
            client = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-failed",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-1", "01-spec", "# Spec")
            pending = state.pending_intents("run-failed")

        self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(len(pending), 1)

    def test_failed_ku_query_returns_stable_reason_without_write_intent(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = KuClient(
                transport=FakeTransport([transport_error("OBJECT_NOT_FOUND")]),
                state_store=state,
                run_id="run-query",
                repo_id="repo-1",
                username="tester",
            )

            result = client.create_artifact("root-missing", "01-spec", "# Spec")

        self.assertEqual(result["reason_code"], "OBJECT_NOT_FOUND")

    def test_failed_ku_index_edit_leaves_intent_for_recovery(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = KuClient(
                transport=FakeTransport(
                    [
                        ku_content("root-1", "# Run index"),
                        ku_version("root-1", 7, 0),
                        transport_error("CLI_PROCESS_FAILED"),
                    ]
                ),
                state_store=state,
                run_id="run-edit-fail",
                repo_id="repo-1",
                username="tester",
            )

            result = client.update_index("root-1", entry)
            recovery = state.pending_intents("run-edit-fail")

        self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(len(recovery), 1)

    def test_failed_ku_index_publish_leaves_only_publish_intent_pending(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = KuClient(
                transport=FakeTransport(
                    [
                        ku_content("root-1", "# Run index"),
                        ku_version("root-1", 7, 0),
                        {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                        ku_content("root-1", f"# Run index\n\n{indexed}"),
                        ku_version("root-1", 8, 4),
                        transport_error("CLI_PROCESS_FAILED"),
                    ]
                ),
                state_store=state,
                run_id="run-publish-fail",
                repo_id="repo-1",
                username="tester",
            )

            result = client.update_index("root-1", entry)
            pending = state.pending_intents("run-publish-fail")

        self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
        self.assertEqual([item["operation"] for item in pending], ["ku.document.publish"])

    def test_publish_receipt_is_withheld_when_latest_version_is_still_a_draft(self):
        if KuClient is None:
            self.fail("KuClient is not implemented")
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": "a" * 64,
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            client = KuClient(
                transport=FakeTransport(
                    [
                        ku_content("root-1", "# Run index"),
                        ku_version("root-1", 7, 0),
                        {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                        ku_content("root-1", f"# Run index\n\n{indexed}"),
                        ku_version("root-1", 8, 4),
                        {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                        ku_content("root-1", f"# Run index\n\n{indexed}"),
                        ku_version("root-1", 9, 4),
                    ]
                ),
                state_store=state,
                run_id="run-draft",
                repo_id="repo-1",
                username="tester",
            )

            result = client.update_index("root-1", entry)
            pending = state.pending_intents("run-draft")

        self.assertEqual(result["reason_code"], "KU_PUBLISH_VERIFICATION_FAILED")
        self.assertEqual([item["operation"] for item in pending], ["ku.document.publish"])


class IcodeClientTests(unittest.TestCase):
    def test_callable_submit_backend_is_rejected(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "ICODE_RUNTIME_REQUIRED"):
            IcodeClient(lambda payload: calls.append(payload) or {"revision": "r1"})

        self.assertEqual(calls, [])


class IpipeClientTests(unittest.TestCase):
    def test_callable_pipeline_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "IPIPE_RUNTIME_REQUIRED"):
            IpipeClient(lambda action, payload: {"action": action, **payload})

    def test_callable_pipeline_backend_cannot_filter_and_write(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "IPIPE_RUNTIME_REQUIRED"):
            IpipeClient(
                lambda action, payload: calls.append((action, payload)) or {"build_id": "b1"}
            )
        self.assertEqual(calls, [])


class ReviewProviderTests(unittest.TestCase):
    def test_timeout_is_incomplete(self):
        def timeout(_change_set):
            raise TimeoutError("review timed out")

        result = ReviewProvider(timeout).review({"change_set_id": "c1"})

        self.assertEqual(result["verdict"], "INCOMPLETE")
        self.assertEqual(result["reason_code"], "REVIEW_PROVIDER_TIMEOUT")


class ApprovalChannelsTests(unittest.TestCase):
    def test_channel_failure_is_not_silently_ignored(self):
        def fail(_request):
            raise RuntimeError("offline")

        channels = ApprovalChannels({"comate": lambda request: request, "infoflow": fail})

        with self.assertRaisesRegex(RuntimeError, "APPROVAL_CHANNEL_FAILED:infoflow"):
            channels.publish({"approval_id": "a1", "input_hash": "h1"})


if __name__ == "__main__":
    unittest.main()
