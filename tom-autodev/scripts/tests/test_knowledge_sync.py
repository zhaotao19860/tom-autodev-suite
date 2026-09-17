import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery import Recovery
from phase_document import canonical_appendix
from state_store import StateStore
from clients.icafe_client import CafeClient
from clients.ku_client import KuClient

from test_adapters import (
    FakeTransport,
    ku_content,
    ku_repo,
    ku_repo_document,
    ku_url,
    ku_version,
    transport_error,
)

try:
    from knowledge_sync import KnowledgeSync
except ModuleNotFoundError:
    KnowledgeSync = None


class FakeKuClient:
    def __init__(self, *, root=None, child=None, index=None):
        self.root = root or {
            "ok": True,
            "reason_code": "OK",
            "doc_id": "root-1",
            "url": "https://ku.example.test/root-1",
            "version": "2",
            "evidence_refs": ["ku:root-1/2"],
        }
        self.child = child or {
            "ok": True,
            "reason_code": "OK",
            "doc_id": "child-1",
            "url": "https://ku.example.test/child-1",
            "version": "4",
            "content_hash": None,
            "evidence_refs": ["ku:child-1/4"],
        }
        self.index = index or {
            "ok": True,
            "reason_code": "OK",
            "doc_id": "root-1",
            "version": "9",
            "evidence_refs": ["ku:root-1/9"],
        }
        self.calls = []

    def ensure_run_root(self, parent_doc_id, title, markdown):
        self.calls.append(("root", parent_doc_id, title, markdown))
        return dict(self.root)

    def create_artifact(self, parent_doc_id, title, markdown):
        self.calls.append(("create", parent_doc_id, title, markdown))
        result = dict(self.child)
        if result.get("content_hash") is None:
            result["content_hash"] = hashlib.sha256(markdown.encode()).hexdigest()
        return result

    def update_index(self, doc_id, entry):
        self.calls.append(("index", doc_id, dict(entry)))
        return dict(self.index)


class FakeCafeClient:
    def __init__(self, result=None):
        self.result = result or {
            "ok": True,
            "reason_code": "OK",
            "comment_id": 91,
            "evidence_refs": ["icafe:BGW-1/91"],
        }
        self.calls = []

    def comment(self, card_id, content, idempotency_key):
        self.calls.append((card_id, content, idempotency_key))
        return dict(self.result)


class KnowledgeSyncTests(unittest.TestCase):
    def test_publish_phase_persists_canonical_ku_and_icafe_evidence_before_success(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            ku = FakeKuClient()
            cafe = FakeCafeClient()
            sync = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-1",
            )
            artifact = {
                "title": "01-spec",
                "markdown": "# Spec",
                "content_hash": hashlib.sha256(b"# Spec").hexdigest(),
            }

            result = sync.publish_phase("run-1", artifact)
            pending = state.pending_intents("run-1")
            with state._connect() as connection:
                durable_receipt = json.loads(connection.execute(
                    "SELECT response_json FROM receipts"
                ).fetchone()[0])

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["evidence_refs"],
            ["ku:child-1/4", "ku:root-1/9", "icafe:BGW-1/91"],
        )
        self.assertEqual(pending, [])
        self.assertEqual(result["child_url"], "https://ku.example.test/child-1")
        self.assertEqual(durable_receipt["child_url"], result["child_url"])
        self.assertEqual([call[0] for call in ku.calls], ["create", "index"])
        self.assertEqual(len(cafe.calls), 1)

    def test_missing_child_receipt_keeps_phase_incomplete_and_never_updates_index(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient(child={"ok": False, "reason_code": "KU_CREATE_VERIFICATION_FAILED"})
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            result = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-2",
            ).publish_phase(
                "run-2",
                {
                    "title": "02-tasks",
                    "markdown": "# Tasks",
                    "content_hash": hashlib.sha256(b"# Tasks").hexdigest(),
                },
            )

        self.assertEqual(result["reason_code"], "KU_CREATE_VERIFICATION_FAILED")
        self.assertEqual([call[0] for call in ku.calls], ["create"])

    def test_root_verification_failure_prevents_icafe_comment(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient(index={"ok": False, "reason_code": "KU_INDEX_VERIFICATION_FAILED"})
        cafe = FakeCafeClient()
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            result = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-3",
            ).publish_phase(
                "run-3",
                {
                    "title": "03-plan",
                    "markdown": "# Plan",
                    "content_hash": hashlib.sha256(b"# Plan").hexdigest(),
                },
            )

        self.assertEqual(result["reason_code"], "KU_INDEX_VERIFICATION_FAILED")
        self.assertEqual(cafe.calls, [])

    def test_a_refused_title_does_not_park_the_run_in_recovery(self):
        """KU refuses a title that already holds different content, writing nothing.

        A re-cut phase hits exactly this, and an intent left open over an operation
        that provably did not happen would block every later phase behind recovery.
        """
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient(child={"ok": False, "reason_code": "KU_IMMUTABLE_CONFLICT"})
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            result = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-9",
            ).publish_phase(
                "run-9",
                {
                    "title": "03-tasks",
                    "markdown": "# Tasks",
                    "content_hash": hashlib.sha256(b"# Tasks").hexdigest(),
                },
            )
            recovery = Recovery(database).resume("run-9")

        self.assertEqual(result["reason_code"], "KU_IMMUTABLE_CONFLICT")
        self.assertEqual(recovery["status"], "READY")

    def test_failed_icafe_comment_keeps_phase_incomplete_without_final_receipt(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        cafe = FakeCafeClient({"ok": False, "reason_code": "ICAFE_COMMENT_VERIFICATION_FAILED"})
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            result = KnowledgeSync(
                state_store=state,
                ku_client=FakeKuClient(),
                cafe_client=cafe,
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-4",
            ).publish_phase(
                "run-4",
                {
                    "title": "04-review",
                    "markdown": "# Review",
                    "content_hash": hashlib.sha256(b"# Review").hexdigest(),
                },
            )
            recovery = Recovery(database).resume("run-4")

        self.assertEqual(result["reason_code"], "ICAFE_COMMENT_VERIFICATION_FAILED")
        self.assertEqual(recovery["status"], "QUERY_REQUIRED")
        self.assertFalse(recovery["retry_allowed"])

    def test_invalid_or_noncanonical_receipts_never_complete_phase(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient(
            child={
                "ok": True,
                "doc_id": "child-1",
                "url": "https://ku.example.test/child-1",
                "version": "4",
                "content_hash": hashlib.sha256(b"# Summary").hexdigest(),
                "evidence_refs": ["https://ku.example.test/child-1"],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            result = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-5",
            ).publish_phase(
                "run-5",
                {
                    "title": "05-summary",
                    "markdown": "# Summary",
                    "content_hash": hashlib.sha256(b"# Summary").hexdigest(),
                },
            )

        self.assertEqual(result["reason_code"], "KNOWLEDGE_EVIDENCE_INVALID")

    def test_sync_intent_payload_does_not_persist_raw_markdown(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        sentinel = "raw-content-must-not-be-durable"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            KnowledgeSync(
                state_store=state,
                ku_client=FakeKuClient(),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-6",
            ).publish_phase(
                "run-6",
                {
                    "title": "06-result",
                    "markdown": sentinel,
                    "content_hash": hashlib.sha256(sentinel.encode()).hexdigest(),
                },
            )

            durable = database.read_bytes()

        self.assertNotIn(sentinel.encode(), durable)

    def test_phase_never_succeeds_while_a_lower_external_intent_is_pending(self):
        markdown = "# Spec"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            lower = state.intent(
                "run-lower-pending",
                "ku.document.publish",
                "ku-publish:child-pending:hash",
                {"doc_id": "child-pending", "content_hash": "a" * 64},
            )
            result = KnowledgeSync(
                state_store=state,
                ku_client=FakeKuClient(),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-lower-pending",
            ).publish_phase(
                "run-lower-pending",
                {
                    "title": "01-spec",
                    "markdown": markdown,
                    "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(result["intent_id"], lower["intent_id"])

    def test_publishing_the_same_artifact_again_after_an_abandonment_proceeds(self):
        """Abandoning a stuck publish must not close that artifact off for good.

        The change set for T3 was published into a document that came back holding only
        its title. Abandoning that write cleared the block, but the publish is keyed by
        the artifact it publishes, so every later attempt replayed the abandonment and
        the run could not leave IMPLEMENT even after the cause was fixed.
        """
        markdown = "# 05-change-set/T3-r2"
        artifact = {
            "title": "05-change-set/T3-r2",
            "markdown": markdown,
            "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            ku = FakeKuClient(child={"ok": False, "reason_code": "KU_CHILD_CONTENT_UNSETTLED"})
            sync = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-abandon",
            )
            stuck = sync.publish_phase("run-abandon", artifact)
            pending = state.pending_intents("run-abandon")
            for intent in pending:
                state.abandon_intent(intent["intent_id"], "文档只落下标题行，改名让出标题", "owner")

            sync.ku = FakeKuClient()
            retried = sync.publish_phase("run-abandon", artifact)
            results = state.external_results("run-abandon")

        self.assertFalse(stuck["ok"])
        self.assertEqual(len(pending), 1)
        self.assertTrue(retried["ok"])
        # The abandoned attempt keeps its own account beside the one that succeeded.
        self.assertEqual(
            sorted(
                bool(result["receipt"]["response"].get("abandoned")) for result in results
            ),
            [False, True],
        )

    def test_artifact_hash_mismatch_is_rejected_before_any_external_call(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient()
        cafe = FakeCafeClient()
        with tempfile.TemporaryDirectory() as directory:
            result = KnowledgeSync(
                state_store=StateStore(Path(directory) / "state.sqlite"),
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-7",
            ).publish_phase(
                "run-7", {"title": "07-invalid", "markdown": "# Actual", "content_hash": "0" * 64}
            )

        self.assertEqual(result["reason_code"], "ARTIFACT_HASH_MISMATCH")
        self.assertEqual(ku.calls, [])
        self.assertEqual(cafe.calls, [])

    def test_profile_bound_sync_creates_one_run_root_and_indexes_only_that_root(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient()
        cafe = FakeCafeClient()
        with tempfile.TemporaryDirectory() as directory:
            sync = KnowledgeSync(
                state_store=StateStore(Path(directory) / "state.sqlite"),
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id=None,
                project_parent_doc_id="project-parent",
                run_root_title="BGW-1-run-8-研发测试协作",
                card_id="BGW-1",
                run_id="run-8",
            )
            markdown = "# Spec"

            result = sync.publish_phase(
                "run-8",
                {
                    "title": "01-spec",
                    "markdown": markdown,
                    "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(ku.calls[0][0:3], ("root", "project-parent", "BGW-1-run-8-研发测试协作"))
        self.assertEqual(ku.calls[1][0:2], ("create", "root-1"))
        self.assertEqual(ku.calls[2][0:2], ("index", "root-1"))

    def test_run_root_body_reads_as_a_directory_people_can_use(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        ku = FakeKuClient()
        cafe = FakeCafeClient()
        with tempfile.TemporaryDirectory() as directory:
            sync = KnowledgeSync(
                state_store=StateStore(Path(directory) / "state.sqlite"),
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id=None,
                project_parent_doc_id="project-parent",
                run_root_title="BGW-1-run-8-研发测试协作",
                card_id="BGW-1",
                run_id="run-8",
            )
            markdown = "# Spec"

            sync.publish_phase(
                "run-8",
                {
                    "title": "01-spec",
                    "markdown": markdown,
                    "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
                },
            )

        body = ku.calls[0][3]
        self.assertTrue(body.startswith("# BGW-1-run-8-研发测试协作\n\n"))
        self.assertIn("BGW-1", body)
        self.assertIn("run-8", body)
        self.assertIn("## 阶段产物", body)
        # Entries are appended after the last block, so the section note has to be it.
        self.assertTrue(body.rstrip().endswith("请不要手工修改。"))

    def test_from_profile_derives_the_ku_target_from_the_project(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        valid = {
            "project_id": "bgw",
            "knowledge_sources": [
                {
                    "provider": "ku",
                    "repo_id": "WoXegIdYRe",
                    "parent_doc_id": "requirement-doc",
                }
            ],
        }
        invalid = json.loads(json.dumps(valid))
        invalid["project_id"] = "unregistered"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            sync = KnowledgeSync.from_profile(
                valid,
                run_id="abcdef1234567890",
                card_id="BGW-1",
                state_store=state,
                ku_transport=FakeTransport([]),
                cafe_transport=FakeTransport([]),
                username="tester",
                cafe_preflight=False,
            )

            with self.assertRaisesRegex(ValueError, "PROJECT_KU_TARGET_INVALID"):
                KnowledgeSync.from_profile(
                    invalid,
                    run_id="abcdef1234567890",
                    card_id="BGW-1",
                    state_store=state,
                    ku_transport=FakeTransport([]),
                    cafe_transport=FakeTransport([]),
                    username="tester",
                    cafe_preflight=False,
                )

        self.assertEqual(sync.project_parent_doc_id, "I15ClP2KW4ZGAK")
        self.assertEqual(sync.ku.repo_id, "sX0BTOBWJX")
        self.assertEqual(sync.run_root_title, "BGW-1-abcdef123456-研发测试协作")

    def test_profile_bound_sync_rejects_a_different_run_before_root_or_intent_calls(self):
        profile = {
            "project_id": "bgw",
            "knowledge_sources": [
                {
                    "provider": "ku",
                    "repo_id": "sX0BTOBWJX",
                    "parent_doc_id": "I15ClP2KW4ZGAK",
                }
            ],
        }
        markdown = "# Spec"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            ku_transport = FakeTransport([])
            cafe_transport = FakeTransport([])
            sync = KnowledgeSync.from_profile(
                profile,
                run_id="run-bound",
                card_id="BGW-1",
                state_store=state,
                ku_transport=ku_transport,
                cafe_transport=cafe_transport,
                username="tester",
                cafe_preflight=False,
            )

            result = sync.publish_phase(
                "run-other",
                {
                    "title": "01-spec",
                    "markdown": markdown,
                    "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
                },
            )
            bound_pending = state.pending_intents("run-bound")
            other_pending = state.pending_intents("run-other")

        self.assertEqual(result["reason_code"], "RUN_ID_MISMATCH")
        self.assertEqual(ku_transport.calls, [])
        self.assertEqual(cafe_transport.calls, [])
        self.assertEqual(bound_pending, [])
        self.assertEqual(other_pending, [])

    def test_full_sync_retry_reconciles_pending_index_edit_without_duplicate_child(self):
        if KnowledgeSync is None:
            self.fail("KnowledgeSync is not implemented")
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
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
                    {"returnCode": 200, "success": True, "result": {"docGuid": "child-1"}},
                    ku_content("child-1", marked),
                    ku_version("child-1", 4, 0),
                    ku_content("root-1", "# Run index"),
                    ku_version("root-1", 7, 0),
                    transport_error("CLI_TIMEOUT"),
                    ku_content("child-1", marked),
                    ku_version("child-1", 4, 0),
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 8, 4),
                    {"returnCode": 200, "success": True, "result": {"docGuid": "root-1"}},
                    ku_content("root-1", f"# Run index\n\n{indexed}"),
                    ku_version("root-1", 9, 0),
                ]
            )
            ku = KuClient(
                transport=transport,
                state_store=state,
                run_id="run-e2e-retry",
                repo_id="repo-1",
                username="tester",
            )
            cafe = FakeCafeClient()
            sync = KnowledgeSync(
                state_store=state,
                ku_client=ku,
                cafe_client=cafe,
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-e2e-retry",
            )
            artifact = {
                "title": "01-spec",
                "markdown": markdown,
                "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
            }

            first = sync.publish_phase("run-e2e-retry", artifact)
            repeated = sync.publish_phase("run-e2e-retry", artifact)
            pending = state.pending_intents("run-e2e-retry")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "create-doc" for call in transport.calls), 1)
        self.assertEqual(sum(call[0][1] == "edit-content" for call in transport.calls), 1)

    def test_full_sync_retry_reconciles_unknown_child_create_without_duplicate_child(self):
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
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
            sync = KnowledgeSync(
                state_store=state,
                ku_client=KuClient(
                    transport=transport,
                    state_store=state,
                    run_id="run-create-retry",
                    repo_id="repo-1",
                    username="tester",
                ),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-create-retry",
            )
            artifact = {
                "title": "01-spec",
                "markdown": markdown,
                "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
            }

            first = sync.publish_phase("run-create-retry", artifact)
            repeated = sync.publish_phase("run-create-retry", artifact)
            pending = state.pending_intents("run-create-retry")

        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "create-doc" for call in transport.calls), 1)

    def test_full_sync_retry_reconciles_unknown_child_publish_without_duplicate_child(self):
        markdown = "# Spec"
        marked = KuClient.marked_markdown("root-1", "01-spec", markdown)
        entry = {
            "title": "01-spec",
            "doc_id": "child-1",
            "url": ku_url("child-1"),
            "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
        }
        indexed = KuClient.index_entry_markdown("root-1", entry)
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
            sync = KnowledgeSync(
                state_store=state,
                ku_client=KuClient(
                    transport=transport,
                    state_store=state,
                    run_id="run-publish-retry",
                    repo_id="repo-1",
                    username="tester",
                ),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-publish-retry",
            )
            artifact = {
                "title": "01-spec",
                "markdown": markdown,
                "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
            }

            first = sync.publish_phase("run-publish-retry", artifact)
            repeated = sync.publish_phase("run-publish-retry", artifact)
            pending = state.pending_intents("run-publish-retry")

        child_publish_calls = [
            call for call in transport.calls
            if call[0][1] == "publish-doc" and call[0][call[0].index("--doc-id") + 1] == "child-1"
        ]
        self.assertEqual(first["reason_code"], "QUERY_REQUIRED")
        self.assertTrue(repeated["ok"])
        self.assertEqual(pending, [])
        self.assertEqual(sum(call[0][1] == "create-doc" for call in transport.calls), 1)
        self.assertEqual(len(child_publish_calls), 1)


    def test_rendered_document_must_carry_the_canonical_json_it_is_hashed_from(self):
        canonical = '{"decision_result":"NO_OPEN_DECISIONS"}'
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        rendered = f"# 01-grill\n\n## 附录\n\n```json\n{canonical}\n```"
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            sync = KnowledgeSync(
                state_store=state,
                ku_client=FakeKuClient(),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-render",
            )

            accepted = sync.publish_phase(
                "run-render",
                {
                    "title": "01-grill", "markdown": rendered,
                    "content_hash": content_hash, "canonical": canonical,
                },
            )
            missing = sync.publish_phase(
                "run-render",
                {
                    "title": "01-grill", "markdown": "# 01-grill\n\n没有附录",
                    "content_hash": content_hash, "canonical": canonical,
                },
            )
            wrong_hash = sync.publish_phase(
                "run-render",
                {
                    "title": "01-grill", "markdown": rendered,
                    "content_hash": "0" * 64, "canonical": canonical,
                },
            )

        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["artifact_hash"], content_hash)
        self.assertEqual(missing["reason_code"], "ARTIFACT_HASH_MISMATCH")
        self.assertEqual(wrong_hash["reason_code"], "ARTIFACT_HASH_MISMATCH")

    def test_a_payload_sized_field_is_carried_elided_but_still_pinned(self):
        """The page may abridge the canonical JSON, but only the one abridgement.

        Inlining a change set's diffs put a 484 KB body on one page and, on the next
        attempt, produced a created document holding nothing but its title line. So the
        appendix elides a payload-sized field behind its own sha256. `canonical_appendix`
        is a pure function of the canonical bytes, so requiring exactly its output still
        makes a page that disagrees with its artifact unpublishable.
        """
        patch = "diff --git a/a b/a\n" + "+x" * 2000
        canonical = json.dumps(
            {"business_patch": patch, "task_id": "T3"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        appendix = canonical_appendix(canonical)
        rendered = f"# 05-change-set/T3\n\n## 附录\n\n```json\n{appendix}\n```"
        tampered = rendered.replace('"task_id":"T3"', '"task_id":"T4"')
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            sync = KnowledgeSync(
                state_store=state,
                ku_client=FakeKuClient(),
                cafe_client=FakeCafeClient(),
                parent_doc_id="root-1",
                card_id="BGW-1",
                run_id="run-elide",
            )
            accepted = sync.publish_phase(
                "run-elide",
                {
                    "title": "05-change-set/T3", "markdown": rendered,
                    "content_hash": content_hash, "canonical": canonical,
                },
            )
            unabridged = sync.publish_phase(
                "run-elide",
                {
                    "title": "05-change-set/T3-r2",
                    "markdown": f"```json\n{canonical}\n```",
                    "content_hash": content_hash, "canonical": canonical,
                },
            )
            drifted = sync.publish_phase(
                "run-elide",
                {
                    "title": "05-change-set/T3-r3", "markdown": tampered,
                    "content_hash": content_hash, "canonical": canonical,
                },
            )

        self.assertTrue(accepted["ok"])
        self.assertNotIn(patch, rendered)
        self.assertIn(hashlib.sha256(patch.encode()).hexdigest(), appendix)
        # The un-abridged canonical is no longer what the page must carry, and a page
        # whose surviving fields disagree with the artifact is still refused.
        self.assertEqual(unabridged["reason_code"], "ARTIFACT_HASH_MISMATCH")
        self.assertEqual(drifted["reason_code"], "ARTIFACT_HASH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
