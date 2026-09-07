import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifact_store import ArtifactStore
from evidence_gate import EvidenceGate
from phase_protocol import PhaseProtocol
from state_store import StateStore
from transition_policy import TransitionPolicy
from test_schema_validation import specialized_examples


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def strict_collaboration_binding(run_id, snapshot):
    roles = {
        "development": ["dev@baidu.com"],
        "test": ["qa@baidu.com"],
        "project": ["pm@baidu.com"],
    }
    return {
        "run_id": run_id,
        "project": "bgw",
        "card_id": "BGW-1",
        "card_title": snapshot["title"],
        "card_content_hash": snapshot["content_hash"],
        "profile_hash": "a" * 64,
        "group_topic": "Resolver-cha",
        "group_name": "BGW-1-Resolver-cha",
        "owner": "pm@baidu.com",
        "roles": roles,
        "member_snapshot": ["dev@baidu.com", "pm@baidu.com", "qa@baidu.com"],
        "friendlyLevel": 3,
        "session_idempotency_key": f"infoflow.group.create:{run_id}",
    }


def strict_intake_payload(run_id, snapshot):
    binding = strict_collaboration_binding(run_id, snapshot)
    prerequisites = {
        "requirement_id": "BGW-1",
        "project": "bgw",
        "profile_hash": "a" * 64,
        "requirement_snapshot": copy.deepcopy(snapshot),
        "collaboration_binding": copy.deepcopy(binding),
    }
    return {**prerequisites, "g0_input_hash": canonical_hash(prerequisites)}


def record_collaboration_receipt(state, run_id, payload, approval_id="approval-1"):
    binding = payload["collaboration_binding"]
    request = {
        key: copy.deepcopy(binding[key])
        for key in (
            "run_id", "project", "card_id", "profile_hash", "group_name", "owner",
            "member_snapshot", "roles", "friendlyLevel", "card_content_hash",
        )
    }
    request.update({"input_hash": payload["g0_input_hash"], "approval_id": approval_id})
    claimed = state.claim_intent(
        run_id, "infoflow.group.create", binding["session_idempotency_key"], request
    )
    state.receipt(claimed["intent"]["intent_id"], {
        "run_id": run_id,
        "group_id": "group-7",
        "group_name": binding["group_name"],
        "owner": binding["owner"],
        "roles": copy.deepcopy(binding["roles"]),
        "member_snapshot": copy.deepcopy(binding["member_snapshot"]),
        "bot_id": "bot-7",
        "message_receipts": [],
        "approval_requests": [payload["g0_input_hash"]],
        "g0_approval_id": approval_id,
    }, [])


def production_profile():
    return {
        "project_id": "bgw",
        "business_repos": [{
            "path": "/repo/business", "module": "resolver", "branch": "main", "lock": "business-main",
        }],
        "test_repo": {
            "path": "/repo/tests", "module": "resolver-tests", "branch": "main", "lock": "tests-main",
        },
        "language_skill": "/skills/language",
        "project_skill": "/skills/project",
        "knowledge_sources": [{
            "provider": "ku", "repository": "knowledge", "revision": "r1",
            "search_scope": "project", "priority": 1, "repo_id": "repo-1", "parent_doc_id": "parent-1",
        }],
        "review_provider": {"kind": "source-only", "command": "review"},
        "pipeline_profile": {
            "pipeline_id": "pipe-1", "allowed_parameters": [], "stage_classes": ["unit"],
            "release_rule": "all stages pass",
        },
        "environment_profile": {
            "runner": "linux", "os_arch": "linux/amd64", "image_digest": "sha256:env",
            "toolchain_digest": "sha256:tools", "hardware_or_simulator": "remote",
            "data": "fixture", "services": "none", "capacity": "small",
        },
        "approval_channels": {
            "comate": {"channel": "comate-review"}, "infoflow": {"channel": "group-1"},
            "role_members": {
                "development": ["dev@example.test"], "test": ["test@example.test"],
                "project": ["owner@example.test"],
            },
        },
    }


class ProductionShapeKnowledgeSync:
    def __init__(self):
        self.calls = []

    def publish_phase(self, run_id, artifact):
        self.calls.append((run_id, copy.deepcopy(artifact)))
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": run_id,
            "artifact_hash": artifact["content_hash"],
            "child_doc_id": "ku-generated-42",
            "child_url": "https://ku.baidu-int.com/knowledge/ku-generated-42",
            "child_version": "v7",
            "index_doc_id": "run-root",
            "index_version": "v3",
            "comment_id": "comment-9",
            "evidence_refs": [
                "ku:ku-generated-42/v7",
                "ku:run-root/v3",
                "icafe:BGW-1/comment-9",
            ],
        }


class BoundApprovalLedger:
    def __init__(self):
        self.records = {}

    def approve(self, *, approval_id, run_id, gate, input_hash):
        self.records[approval_id] = {
            "approval_id": approval_id,
            "run_id": run_id,
            "action": gate,
            "input_hash": input_hash,
            "effective_decision": "APPROVE",
        }

    def get(self, approval_id):
        return copy.deepcopy(self.records.get(approval_id))


class CallbackKnowledgeSync(ProductionShapeKnowledgeSync):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def publish_phase(self, run_id, artifact):
        result = super().publish_phase(run_id, artifact)
        self.callback()
        return result


class RecoveringKnowledgeSync(ProductionShapeKnowledgeSync):
    def __init__(self):
        super().__init__()
        self.remote_calls = 0
        self.cache = {}
        self.lock = threading.Lock()

    def publish_phase(self, run_id, artifact):
        key = (run_id, artifact["content_hash"])
        with self.lock:
            self.calls.append((run_id, copy.deepcopy(artifact)))
            if key not in self.cache:
                self.remote_calls += 1
                self.cache[key] = ProductionShapeKnowledgeSync().publish_phase(run_id, artifact)
            return copy.deepcopy(self.cache[key])


class CrashBeforeArtifactStore(ArtifactStore):
    def __init__(self, root):
        super().__init__(root)
        self.crash_once = True

    def put_envelope(self, envelope):
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("local artifact write interrupted")
        return super().put_envelope(envelope)


class CrashAfterCommitStateStore(StateStore):
    def __init__(self, database_path):
        super().__init__(database_path)
        self.crash_once = True

    def commit_transition_result(self, *args, **kwargs):
        result = super().commit_transition_result(*args, **kwargs)
        if self.crash_once:
            self.crash_once = False
            raise RuntimeError("response lost after commit")
        return result


class RaisingPendingStateStore(StateStore):
    def __init__(self, database_path, reason):
        super().__init__(database_path)
        self.reason = reason

    def pending_intents(self, run_id):
        raise ValueError(self.reason)


class PhaseProtocolRepairPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state = StateStore(root / "state.sqlite")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.knowledge = ProductionShapeKnowledgeSync()
        self.approvals = BoundApprovalLedger()
        self.snapshot = specialized_examples()["requirement-snapshot"]
        payload = strict_intake_payload("run-1", self.snapshot)
        self.state.transition("run-1", "INTAKE", payload)
        record_collaboration_receipt(self.state, "run-1", payload)
        self.protocol = PhaseProtocol(
            state_store=self.state,
            artifact_store=self.artifacts,
            knowledge_sync=self.knowledge,
            approval_ledger=self.approvals,
            evidence_gate=EvidenceGate(),
            transition_policy=TransitionPolicy(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def draft(self, action, content=None, approval_id="approval-1", source_revisions=None):
        content = copy.deepcopy(content if content is not None else self.snapshot)
        content_hash = canonical_hash(content)
        revisions = copy.deepcopy(
            action["source_revisions"] if source_revisions is None else source_revisions
        )
        approval_input_hash = (
            action["input_hash"]
            if action["phase"] == "INTAKE"
            else canonical_hash({
                "action_id": action["action_id"],
                "task_id": action["task_id"],
                "parent_artifact_hash": action["parent_artifact_hash"],
                "source_revisions": revisions,
                "content_hash": content_hash,
            })
        )
        return {
            "action_id": action["action_id"],
            "source_event_id": action["source_event_id"],
            "host": "comate",
            "run_id": action["run_id"],
            "phase": action["phase"],
            "task_id": action["task_id"],
            "schema_version": "1",
            "input_hash": action["input_hash"],
            "content_hash": content_hash,
            "source_revisions": revisions,
            "parent_artifact_hash": action["parent_artifact_hash"],
            "knowledge_doc_id": None,
            "knowledge_url": None,
            "knowledge_version": None,
            "icafe_comment_id": None,
            "evidence_refs": copy.deepcopy(action.get("source_evidence_refs", [])),
            "approval_id": approval_id,
            "approval_input_hash": approval_input_hash,
            "content": content,
        }

    def test_intake_is_controller_owned_and_g0_binds_real_snapshot_collaboration_inputs(self):
        action = self.protocol.next("run-1")
        changed_state = StateStore(Path(self.temporary.name) / "changed.sqlite")
        changed = strict_intake_payload("run-2", self.snapshot)
        changed["collaboration_binding"]["owner"] = "other@baidu.com"
        changed["collaboration_binding"]["roles"]["project"] = ["other@baidu.com"]
        changed["collaboration_binding"]["member_snapshot"] = [
            "dev@baidu.com", "other@baidu.com", "qa@baidu.com",
        ]
        changed["g0_input_hash"] = canonical_hash({
            key: value for key, value in changed.items() if key != "g0_input_hash"
        })
        changed_state.transition("run-2", "INTAKE", changed)
        changed_protocol = PhaseProtocol(
            state_store=changed_state,
            artifact_store=ArtifactStore(Path(self.temporary.name) / "changed-artifacts"),
            knowledge_sync=self.knowledge,
            approval_ledger=self.approvals,
            evidence_gate=EvidenceGate(),
            transition_policy=TransitionPolicy(),
        )
        changed_action = changed_protocol.next("run-2")

        self.assertEqual((action["controller"], action["child_skill"]), ("intake", None))
        self.assertEqual(action["result_schema"], "requirement-snapshot")
        self.assertEqual(action["required_human_gate"], "G0")
        self.assertEqual(action["content"], self.snapshot)
        self.assertEqual(action["input_hash"], self.state.events("run-1")[0]["payload"]["g0_input_hash"])
        self.assertNotEqual(action["input_hash"], changed_action["input_hash"])

    def test_intake_action_rejects_missing_or_incomplete_prerequisites(self):
        base = strict_intake_payload("run-invalid", self.snapshot)
        cases = {
            "snapshot": lambda payload: payload.pop("requirement_snapshot"),
            "group_name": lambda payload: payload["collaboration_binding"].pop("group_name"),
            "owner": lambda payload: payload["collaboration_binding"].pop("owner"),
            "members": lambda payload: payload["collaboration_binding"].pop("member_snapshot"),
            "session": lambda payload: payload["collaboration_binding"].pop("session_idempotency_key"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                state = StateStore(Path(self.temporary.name) / f"invalid-{name}.sqlite")
                payload = copy.deepcopy(base)
                mutate(payload)
                state.transition(f"run-invalid-{name}", "INTAKE", payload)
                protocol = PhaseProtocol(
                    state_store=state,
                    artifact_store=ArtifactStore(Path(self.temporary.name) / f"invalid-{name}-artifacts"),
                    knowledge_sync=self.knowledge,
                    approval_ledger=self.approvals,
                    evidence_gate=EvidenceGate(),
                    transition_policy=TransitionPolicy(),
                )

                result = protocol.next(f"run-invalid-{name}")

                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason_code"], "INTAKE_PREREQUISITES_INVALID")

    def test_intake_completion_requires_the_exact_durable_collaboration_receipt(self):
        for mutation in ("missing", "group", "approval"):
            with self.subTest(mutation=mutation):
                run_id = f"run-session-{mutation}"
                state = StateStore(Path(self.temporary.name) / f"session-{mutation}.sqlite")
                payload = strict_intake_payload(run_id, self.snapshot)
                state.transition(run_id, "INTAKE", payload)
                if mutation != "missing":
                    record_collaboration_receipt(
                        state, run_id, payload,
                        approval_id="approval-other" if mutation == "approval" else "approval-1",
                    )
                    if mutation == "group":
                        with state._connect() as connection:
                            row = connection.execute(
                                "SELECT intent_id, response_json FROM receipts"
                            ).fetchone()
                            response = json.loads(row["response_json"])
                            response["group_name"] = "other-group"
                            connection.execute(
                                "UPDATE receipts SET response_json = ? WHERE intent_id = ?",
                                (json.dumps(response, sort_keys=True, separators=(",", ":")), row["intent_id"]),
                            )
                approvals = BoundApprovalLedger()
                protocol = PhaseProtocol(
                    state_store=state,
                    artifact_store=ArtifactStore(Path(self.temporary.name) / f"session-{mutation}-artifacts"),
                    knowledge_sync=self.knowledge,
                    approval_ledger=approvals,
                    evidence_gate=EvidenceGate(),
                    transition_policy=TransitionPolicy(),
                )
                action = protocol.next(run_id)
                draft = self.draft(action)
                draft["run_id"] = run_id
                approvals.approve(
                    approval_id="approval-1", run_id=run_id, gate="G0",
                    input_hash=draft["approval_input_hash"],
                )

                result = protocol.validate_result(action, draft)

                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason_code"], "COLLABORATION_SESSION_INVALID")

    def test_draft_remote_identity_is_null_and_content_hash_covers_only_specialized_content(self):
        action = self.protocol.next("run-1")
        draft = self.draft(action)
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=draft["approval_input_hash"],
        )

        validated = self.protocol.validate_result(action, draft)

        self.assertTrue(validated["ok"], validated)
        self.assertEqual(validated["draft"]["content_hash"], canonical_hash(self.snapshot))
        self.assertIsNone(validated["draft"]["knowledge_doc_id"])
        self.assertIsNone(validated["draft"]["knowledge_url"])

    def test_complete_enriches_final_envelope_from_real_publish_receipt(self):
        action = self.protocol.next("run-1")
        draft = self.draft(action)
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=draft["approval_input_hash"],
        )

        result = self.protocol.complete("run-1", draft)
        stored = self.artifacts.latest_phase("run-1", "INTAKE", None)

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.knowledge.calls), 1)
        self.assertEqual(self.knowledge.calls[0][1]["content_hash"], canonical_hash(self.snapshot))
        self.assertEqual(stored["envelope"]["knowledge_doc_id"], "ku-generated-42")
        self.assertEqual(stored["envelope"]["knowledge_url"], "https://ku.baidu-int.com/knowledge/ku-generated-42")
        self.assertEqual(stored["envelope"]["knowledge_version"], "v7")
        self.assertEqual(stored["envelope"]["icafe_comment_id"], "comment-9")
        self.assertEqual(stored["envelope"]["content_hash"], canonical_hash(self.snapshot))

    def test_final_evidence_is_a_stable_union_and_propagates_to_the_next_action(self):
        action = self.protocol.next("run-1")
        draft = self.draft(action)
        draft["evidence_refs"] = ["artifact:source-1", "ku:run-root/v3"]
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=draft["approval_input_hash"],
        )

        result = self.protocol.complete("run-1", draft)
        stored = self.artifacts.latest_phase("run-1", "INTAKE", None)
        next_action = self.protocol.next("run-1")

        expected = [
            "artifact:source-1", "ku:run-root/v3", "ku:ku-generated-42/v7",
            "icafe:BGW-1/comment-9",
        ]
        self.assertTrue(result["ok"], result)
        self.assertEqual(stored["envelope"]["evidence_refs"], expected)
        self.assertEqual(next_action["source_evidence_refs"], expected)

    def test_result_bound_gate_rejects_approval_for_old_content(self):
        intake_action = self.protocol.next("run-1")
        intake = self.draft(intake_action)
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=intake["approval_input_hash"],
        )
        self.assertTrue(self.protocol.complete("run-1", intake)["ok"])
        action = self.protocol.next("run-1")
        old_content = specialized_examples()["decision-log"]
        changed_content = copy.deepcopy(old_content)
        changed_content["decision_result"] = "UNRESOLVED"
        changed_content["status"] = "INCOMPLETE"
        changed_content["unresolved_frontier"] = ["Which timeout?"]
        old = self.draft(action, old_content, approval_id="approval-g1")
        changed = self.draft(action, changed_content, approval_id="approval-g1")
        self.approvals.approve(
            approval_id="approval-g1", run_id="run-1", gate="G1",
            input_hash=old["approval_input_hash"],
        )

        result = self.protocol.validate_result(action, changed)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "APPROVAL_INPUT_MISMATCH")


class StateStoreAtomicCompletionTests(unittest.TestCase):
    def test_compare_and_swap_transition_and_result_are_one_idempotent_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            if not hasattr(state, "commit_transition_result"):
                self.fail("atomic transition/result commit is not implemented")
            source = state.transition("run-cas", "INTAKE", {"profile_hash": "a" * 64})
            result = {"ok": True, "state": "GRILL", "artifact_id": "artifact-1"}

            first = state.commit_transition_result(
                "run-cas", source["event_id"], "GRILL", {"source_event_id": source["event_id"]},
                "complete:run-cas:action-1", result,
            )
            replay = state.commit_transition_result(
                "run-cas", source["event_id"], "GRILL", {"source_event_id": source["event_id"]},
                "complete:run-cas:action-1", result,
            )

            self.assertEqual(first["status"], "COMMITTED")
            self.assertEqual(replay["status"], "REPLAY")
            self.assertEqual(len(state.events("run-cas")), 2)
            self.assertEqual(state.idempotency_result("complete:run-cas:action-1"), first["result"])

    def test_compare_and_swap_rejects_stale_or_changed_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            if not hasattr(state, "commit_transition_result"):
                self.fail("atomic transition/result commit is not implemented")
            source = state.transition("run-cas", "INTAKE", {})
            state.transition("run-cas", "STOPPED", {})

            stale = state.commit_transition_result(
                "run-cas", source["event_id"], "GRILL", {}, "complete:stale", {"ok": True},
            )

            self.assertEqual(stale["status"], "SOURCE_EVENT_MISMATCH")
            self.assertEqual(len(state.events("run-cas")), 2)

    def test_concurrent_compare_and_swap_appends_one_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            if not hasattr(state, "commit_transition_result"):
                self.fail("atomic transition/result commit is not implemented")
            source = state.transition("run-race", "INTAKE", {})
            barrier = threading.Barrier(8)

            def commit(index):
                barrier.wait()
                return StateStore(database).commit_transition_result(
                    "run-race", source["event_id"], "GRILL", {}, "complete:race", {"ok": True},
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(commit, range(8)))

            self.assertEqual(sum(result["status"] == "COMMITTED" for result in results), 1)
            self.assertEqual(len(state.events("run-race")), 2)


class PhaseProtocolRecoveryAndIntegrityTests(PhaseProtocolRepairPublicationTests):
    def approved_draft(self):
        action = self.protocol.next("run-1")
        draft = self.draft(action)
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=draft["approval_input_hash"],
        )
        return action, draft

    def test_changed_duplicate_conflicts_instead_of_replaying_old_success(self):
        _, draft = self.approved_draft()
        first = self.protocol.complete("run-1", draft)
        changed = copy.deepcopy(draft)
        changed["content"]["title"] = "changed duplicate"
        unsigned = {key: value for key, value in changed["content"].items() if key != "content_hash"}
        changed["content"]["content_hash"] = canonical_hash(unsigned)
        changed["content_hash"] = canonical_hash(changed["content"])

        duplicate = self.protocol.complete("run-1", changed)

        self.assertTrue(first["ok"])
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["reason_code"], "COMPLETION_CONFLICT")

    def test_cached_replay_rechecks_artifact_integrity(self):
        _, draft = self.approved_draft()
        first = self.protocol.complete("run-1", draft)
        artifact = self.artifacts.get(first["artifact_id"])
        Path(artifact["path"]).write_bytes(b"corrupt")

        replay = self.protocol.complete("run-1", draft)

        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason_code"], "ARTIFACT_INTEGRITY_FAILED")

    def test_cached_replay_loads_the_exact_older_phase_artifact_after_reentry(self):
        _, draft = self.approved_draft()
        first = self.protocol.complete("run-1", draft)
        newer_content = copy.deepcopy(self.snapshot)
        newer_content["title"] = "Newer intake reentry"
        unsigned = {key: value for key, value in newer_content.items() if key != "content_hash"}
        newer_content["content_hash"] = canonical_hash(unsigned)
        self.artifacts.put_envelope(final_envelope("run-1", "INTAKE", None, newer_content))

        replay = self.protocol.complete("run-1", draft)
        old_artifact = self.artifacts.get(first["artifact_id"])
        Path(old_artifact["path"]).write_bytes(b"corrupt exact old artifact")
        corrupt_replay = self.protocol.complete("run-1", draft)

        self.assertEqual(replay, first)
        self.assertFalse(corrupt_replay["ok"])
        self.assertEqual(corrupt_replay["reason_code"], "ARTIFACT_INTEGRITY_FAILED")

    def test_source_event_changed_during_publish_cannot_advance_stale_action(self):
        _, draft = self.approved_draft()
        callback_sync = CallbackKnowledgeSync(
            lambda: self.state.transition("run-1", "STOPPED", {"reason_code": "CONCURRENT"})
        )
        self.protocol.knowledge = callback_sync

        result = self.protocol.complete("run-1", draft)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "STALE_ACTION")
        self.assertEqual(self.state.events("run-1")[-1]["state"], "STOPPED")

    def test_response_loss_after_atomic_commit_replays_without_remote_duplicate(self):
        root = Path(self.temporary.name)
        crash_state = CrashAfterCommitStateStore(root / "crash.sqlite")
        crash_payload = strict_intake_payload("run-crash", self.snapshot)
        crash_state.transition("run-crash", "INTAKE", crash_payload)
        record_collaboration_receipt(crash_state, "run-crash", crash_payload)
        knowledge = ProductionShapeKnowledgeSync()
        approvals = BoundApprovalLedger()
        protocol = PhaseProtocol(
            state_store=crash_state, artifact_store=ArtifactStore(root / "crash-artifacts"),
            knowledge_sync=knowledge, approval_ledger=approvals,
            evidence_gate=EvidenceGate(), transition_policy=TransitionPolicy(),
        )
        action = protocol.next("run-crash")
        draft = self.draft(action)
        draft["run_id"] = "run-crash"
        approvals.approve(
            approval_id="approval-1", run_id="run-crash", gate="G0",
            input_hash=draft["approval_input_hash"],
        )

        lost = protocol.complete("run-crash", draft)
        replay = protocol.complete("run-crash", draft)

        self.assertEqual(lost["reason_code"], "PHASE_COMPLETION_FAILED")
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(len(knowledge.calls), 1)
        self.assertEqual(len(crash_state.events("run-crash")), 2)

    def test_crash_after_publish_reuses_durable_publish_result_without_remote_duplicate(self):
        root = Path(self.temporary.name)
        knowledge = RecoveringKnowledgeSync()
        protocol = PhaseProtocol(
            state_store=self.state, artifact_store=CrashBeforeArtifactStore(root / "crash-before-artifact"),
            knowledge_sync=knowledge, approval_ledger=self.approvals,
            evidence_gate=EvidenceGate(), transition_policy=TransitionPolicy(),
        )
        action = protocol.next("run-1")
        draft = self.draft(action)
        self.approvals.approve(
            approval_id="approval-1", run_id="run-1", gate="G0",
            input_hash=draft["approval_input_hash"],
        )

        crashed = protocol.complete("run-1", draft)
        recovered = protocol.complete("run-1", draft)

        self.assertEqual(crashed["reason_code"], "PHASE_COMPLETION_FAILED")
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual((len(knowledge.calls), knowledge.remote_calls), (2, 1))

    def test_concurrent_complete_calls_reconcile_to_one_result_and_one_transition(self):
        knowledge = RecoveringKnowledgeSync()
        self.protocol.knowledge = knowledge
        _, draft = self.approved_draft()
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait()
            return self.protocol.complete("run-1", copy.deepcopy(draft))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: complete(), range(2)))

        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.state.events("run-1")), 2)
        self.assertEqual(knowledge.remote_calls, 1)

    def test_profile_predecessor_and_approval_are_rechecked_after_publish(self):
        for mutation, expected in (
            ("profile", "PROFILE_CONFLICT"),
            ("predecessor", "PARENT_ARTIFACT_MISMATCH"),
            ("approval", "APPROVAL_REQUIRED"),
        ):
            with self.subTest(mutation=mutation):
                run_id = f"run-recheck-{mutation}"
                profile = Path(self.temporary.name) / f"{mutation}.yaml"
                profile.write_bytes(b"profile-v1")
                snapshot = specialized_examples()["requirement-snapshot"]
                self.artifacts.put_envelope(final_envelope(run_id, "INTAKE", None, snapshot))
                self.state.transition(run_id, "GRILL", {
                    "requirement_id": "BGW-1", "profile_path": str(profile),
                    "profile_hash": hashlib.sha256(b"profile-v1").hexdigest(),
                })
                protocol = PhaseProtocol(
                    state_store=self.state, artifact_store=self.artifacts,
                    knowledge_sync=None, approval_ledger=self.approvals,
                    evidence_gate=EvidenceGate(), transition_policy=TransitionPolicy(),
                )
                action = protocol.next(run_id)
                draft = self.draft(action, specialized_examples()["decision-log"], approval_id=f"approval-{mutation}")
                self.approvals.approve(
                    approval_id=f"approval-{mutation}", run_id=run_id, gate="G1",
                    input_hash=draft["approval_input_hash"],
                )

                def mutate():
                    if mutation == "profile":
                        profile.write_bytes(b"profile-v2")
                    elif mutation == "predecessor":
                        changed = specialized_examples()["requirement-snapshot"]
                        changed["title"] = "Changed"
                        unsigned = {key: value for key, value in changed.items() if key != "content_hash"}
                        changed["content_hash"] = canonical_hash(unsigned)
                        self.artifacts.put_envelope(final_envelope(run_id, "INTAKE", None, changed))
                    else:
                        self.approvals.records[f"approval-{mutation}"]["effective_decision"] = "REJECT"

                protocol.knowledge = CallbackKnowledgeSync(mutate)
                result = protocol.complete(run_id, draft)
                self.assertEqual(result["reason_code"], expected, result)

    def test_cached_replay_rechecks_receipt_and_approval_integrity(self):
        _, draft = self.approved_draft()
        first = self.protocol.complete("run-1", draft)
        self.assertTrue(first["ok"])
        result_key = f"phase-completion:run-1:{draft['action_id']}"
        with self.state._connect() as connection:
            saved = json.loads(connection.execute(
                "SELECT result_json FROM idempotency_results WHERE idempotency_key = ?", (result_key,)
            ).fetchone()[0])
            saved["knowledge_receipt"]["child_url"] = "https://ku.baidu-int.com/knowledge/other"
            connection.execute(
                "UPDATE idempotency_results SET result_json = ? WHERE idempotency_key = ?",
                (json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(",", ":")), result_key),
            )

        receipt_replay = self.protocol.complete("run-1", draft)
        self.assertEqual(receipt_replay["reason_code"], "KU_RECEIPT_MISMATCH")

        saved["knowledge_receipt"]["child_url"] = "https://ku.baidu-int.com/knowledge/ku-generated-42"
        with self.state._connect() as connection:
            connection.execute(
                "UPDATE idempotency_results SET result_json = ? WHERE idempotency_key = ?",
                (json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(",", ":")), result_key),
            )
        self.approvals.records["approval-1"]["effective_decision"] = "REJECT"
        approval_replay = self.protocol.complete("run-1", draft)
        self.assertEqual(approval_replay["reason_code"], "APPROVAL_REQUIRED")

    def test_artifact_store_rolls_back_both_rows_when_phase_index_insert_fails(self):
        _, draft = self.approved_draft()
        self.assertTrue(self.protocol.complete("run-1", draft)["ok"])
        final = copy.deepcopy(self.artifacts.latest_phase("run-1", "INTAKE", None)["envelope"])
        final["run_id"] = "run-atomic"
        final["action_id"] = "b" * 64
        store = ArtifactStore(Path(self.temporary.name) / "atomic-artifacts")
        with store._connect() as connection:
            connection.execute(
                "CREATE TRIGGER fail_phase BEFORE INSERT ON phase_artifacts BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )

        with self.assertRaises(Exception):
            store.put_envelope(final)
        with store._connect() as connection:
            artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            phase_count = connection.execute("SELECT COUNT(*) FROM phase_artifacts").fetchone()[0]

        self.assertEqual((artifact_count, phase_count), (0, 0))

    def test_artifact_store_revalidates_specialized_schema_before_indexing(self):
        _, draft = self.approved_draft()
        self.assertTrue(self.protocol.complete("run-1", draft)["ok"])
        final = copy.deepcopy(self.artifacts.latest_phase("run-1", "INTAKE", None)["envelope"])
        final["run_id"] = "run-invalid"
        final["action_id"] = "c" * 64
        del final["content"]["title"]
        final["content_hash"] = canonical_hash(final["content"])
        store = ArtifactStore(Path(self.temporary.name) / "invalid-artifacts")

        with self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
            store.put_envelope(final)

        self.assertEqual(store.phase_artifacts("run-invalid"), [])

    def test_public_reason_codes_use_allowlist(self):
        for dependency_reason, expected in [
            ("INTENT_CONFLICT", "INTENT_CONFLICT"),
            ("SURPRISE_INTERNAL_CODE", "PHASE_PROTOCOL_INVALID"),
        ]:
            with self.subTest(dependency_reason=dependency_reason):
                state = RaisingPendingStateStore(
                    Path(self.temporary.name) / f"{dependency_reason}.sqlite", dependency_reason
                )
                state.transition("run-reason", "INTAKE", copy.deepcopy(self.state.events("run-1")[0]["payload"]))
                protocol = PhaseProtocol(
                    state_store=state,
                    artifact_store=ArtifactStore(Path(self.temporary.name) / f"{dependency_reason}-artifacts"),
                    knowledge_sync=self.knowledge,
                    approval_ledger=self.approvals,
                    evidence_gate=EvidenceGate(), transition_policy=TransitionPolicy(),
                )
                self.assertEqual(protocol.next("run-reason")["reason_code"], expected)


def final_envelope(run_id, phase, task_id, content, *, parent_hash=None, revisions=None):
    return {
        "action_id": canonical_hash({"run_id": run_id, "phase": phase, "task_id": task_id, "content": content}),
        "source_event_id": "seed-event", "host": "comate", "run_id": run_id,
        "phase": phase, "task_id": task_id, "schema_version": "1",
        "input_hash": "a" * 64, "content_hash": canonical_hash(content),
        "source_revisions": copy.deepcopy(revisions or {}), "parent_artifact_hash": parent_hash,
        "knowledge_doc_id": f"doc-{phase.lower()}-{task_id or 'root'}",
        "knowledge_url": f"https://ku.baidu-int.com/knowledge/doc-{phase.lower()}-{task_id or 'root'}",
        "knowledge_version": "v1", "icafe_comment_id": f"comment-{phase.lower()}-{task_id or 'root'}",
        "evidence_refs": [f"ku:doc-{phase.lower()}-{task_id or 'root'}/v1", f"icafe:BGW-1/comment-{phase.lower()}-{task_id or 'root'}"],
        "approval_id": None, "approval_input_hash": None, "content": copy.deepcopy(content),
    }


def two_node_dag():
    dag = specialized_examples()["task-dag"]
    second = copy.deepcopy(dag["nodes"][0])
    second.update({"task_id": "T-2", "title": "Dependent slice", "acceptance_point_ids": ["AC-2"]})
    dag["nodes"].append(second)
    dag["edges"] = [{"from": "T-1", "to": "T-2"}]
    dag["acceptance_coverage"].append({"acceptance_point_id": "AC-2", "task_ids": ["T-2"]})
    return dag


class PhaseProtocolFrontierAndControllerTests(PhaseProtocolRepairPublicationTests):
    def seed(self, run_id, phase, task_id, content, *, parent_hash=None, revisions=None):
        return self.artifacts.put_envelope(final_envelope(
            run_id, phase, task_id, content, parent_hash=parent_hash, revisions=revisions
        ))

    def test_dependency_frontier_selects_only_nodes_with_passing_prerequisite_reviews(self):
        run_id = "run-frontier"
        self.seed(run_id, "TASKS", None, two_node_dag())
        self.state.transition(run_id, "WORKSPACE", {"profile_hash": "a" * 64})

        first = self.protocol.next(run_id)
        self.assertEqual(first["task_id"], "T-1")

        self.seed(run_id, "REVIEW", "T-1", specialized_examples()["review"], revisions={"business": "r2", "tests": "t2"})
        self.state.transition(run_id, "WORKSPACE", {"profile_hash": "a" * 64})
        second = self.protocol.next(run_id)

        self.assertEqual(second["task_id"], "T-2")

    def review_run(self, run_id, task_id, review_content, *, prior_review=False):
        self.seed(run_id, "TASKS", None, two_node_dag())
        if prior_review:
            self.seed(run_id, "REVIEW", "T-1", specialized_examples()["review"], revisions={"business": "r2", "tests": "t2"})
        change = specialized_examples()["change-set"]
        change["task_id"] = task_id
        change["candidate_hash"] = canonical_hash({
            key: value for key, value in change.items() if key != "candidate_hash"
        })
        self.seed(run_id, "IMPLEMENT", task_id, change, revisions={"business": "r2", "tests": "t2"})
        self.state.transition(run_id, "REVIEW", {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64, "task_id": task_id,
            "source_revisions": {"business": "r2", "tests": "t2"},
        })
        action = self.protocol.next(run_id)
        review_content = copy.deepcopy(review_content)
        review_content["task_id"] = task_id
        review_content["change_set_hash"] = change["candidate_hash"]
        review_content["baseline_revisions"] = copy.deepcopy(change["baseline_revisions"])
        draft = self.draft(action, review_content, approval_id=None)
        return self.protocol.complete(run_id, draft)

    def test_passing_review_routes_to_next_task_then_submit_only_after_all_nodes(self):
        first = self.review_run("run-review-first", "T-1", specialized_examples()["review"])
        second_review = specialized_examples()["review"]
        second_review["task_id"] = "T-2"
        second = self.review_run("run-review-last", "T-2", second_review, prior_review=True)

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual((first["state"], first["task_id"]), ("WORKSPACE", "T-1"))
        self.assertEqual(self.state.events("run-review-first")[-1]["payload"]["task_id"], "T-2")
        self.assertEqual(second["state"], "SUBMIT")

    def test_review_reject_blocking_and_incomplete_have_distinct_fail_closed_routes(self):
        rejected = specialized_examples()["review"]
        rejected["verdict"] = "REJECT"
        blocking = specialized_examples()["review"]
        blocking["findings"] = [{
            "id": "F-1", "axis": "standards", "severity": "P0", "location": "src/a.cc:1",
            "evidence": "broken", "acceptance_point_ids": ["AC-1"], "blocking": True,
        }]
        blocking["axes"]["standards"]["finding_ids"] = ["F-1"]
        blocking["verdict"] = "REJECT"
        incomplete = specialized_examples()["review"]
        incomplete["verdict"] = "INCOMPLETE"
        incomplete["completeness_state"] = "INCOMPLETE"
        incomplete["axes"]["spec"]["complete"] = False

        reject_result = self.review_run("run-review-reject", "T-1", rejected)
        blocking_result = self.review_run("run-review-block", "T-1", blocking)
        incomplete_result = self.review_run("run-review-incomplete", "T-1", incomplete)

        self.assertTrue(reject_result["ok"], reject_result)
        self.assertTrue(blocking_result["ok"], blocking_result)
        self.assertTrue(incomplete_result["ok"], incomplete_result)
        self.assertEqual(reject_result["state"], "DIAGNOSE")
        self.assertEqual(blocking_result["state"], "DIAGNOSE")
        self.assertEqual((incomplete_result["state"], incomplete_result["reason_code"]), ("STOPPED", "REVIEW_INCOMPLETE"))


    def test_release_descriptor_is_not_a_phase_result_action_or_run_summary(self):
        run_id = "run-controller-release"
        self.seed(
            run_id, "IPIPE", None, specialized_examples()["ipipe-evidence"],
            revisions={"business": "r2", "tests": "t2"},
        )
        submission = self.artifacts.put(
            run_id, "submission", b"submitted", {"revision": "r2"}
        )
        self.state.transition(run_id, "RELEASE", {
            "profile_hash": "a" * 64,
            "source_revisions": {"business": "r2", "tests": "t2"},
            "submission_artifact_id": submission["artifact_id"],
            "submission_hash": submission["sha256"],
        })

        action = self.protocol.next(run_id)

        self.assertTrue(action["ok"], action)
        self.assertIsNone(action["result_schema"])
        self.assertFalse(action["completion_predicate"]["accepts_phase_result"])
        self.assertEqual(
            self.protocol.validate_result(action, {})["reason_code"],
            "CONTROLLER_ACTION_NOT_COMPLETABLE",
        )

    def test_incomplete_diagnosis_routes_to_stopped_with_explicit_reason(self):
        revisions = {"business": "r2", "tests": "t2"}
        run_id = "run-diagnosis-incomplete"
        self.seed(run_id, "IMPLEMENT", "T-1", specialized_examples()["change-set"], revisions=revisions)
        self.state.transition(run_id, "DIAGNOSE", {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64, "task_id": "T-1",
            "source_revisions": revisions,
        })
        action = self.protocol.next(run_id)
        content = specialized_examples()["diagnosis"]
        content.update({
            "evidence_state": "INSUFFICIENT", "route": "DIAGNOSIS_INCOMPLETE",
            "hypothesis": None, "repair_direction": None, "repair_plan": [],
            "repair_diff_hash": None,
        })
        draft = self.draft(action, content, approval_id="approval-diagnosis-incomplete")
        self.approvals.approve(
            approval_id="approval-diagnosis-incomplete", run_id=run_id, gate="G6",
            input_hash=draft["approval_input_hash"],
        )

        result = self.protocol.complete(run_id, draft)

        self.assertTrue(result["ok"], result)
        self.assertEqual((result["state"], result["reason_code"]), ("STOPPED", "DIAGNOSIS_INCOMPLETE"))


class PhaseProtocolPredecessorBindingTests(PhaseProtocolFrontierAndControllerTests):
    def validate_draft(self, action, content, *, source_revisions=None):
        approval_id = None if action["required_human_gate"] is None else f"approval-{action['phase'].lower()}"
        draft = self.draft(
            action, content, approval_id=approval_id, source_revisions=source_revisions
        )
        if approval_id is not None:
            self.approvals.approve(
                approval_id=approval_id, run_id=action["run_id"],
                gate=action["required_human_gate"], input_hash=draft["approval_input_hash"],
            )
        return self.protocol.validate_result(action, draft)

    def action_for(self, run_id, phase, predecessor_phase, predecessor_content, *, task_id=None, revisions=None):
        self.seed(
            run_id, predecessor_phase, task_id if predecessor_phase in {"PLAN", "IMPLEMENT", "REVIEW"} else None,
            predecessor_content, revisions=revisions,
        )
        self.state.transition(run_id, phase, {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64,
            "task_id": task_id, "source_revisions": copy.deepcopy(revisions or {}),
        })
        action = self.protocol.next(run_id)
        self.assertTrue(action["ok"], action)
        return action

    def test_spec_traceability_covers_root_snapshot_and_internal_ids(self):
        run_id = "run-bind-spec"
        snapshot = specialized_examples()["requirement-snapshot"]
        snapshot["acceptance"].append("AC-2")
        unsigned = {key: value for key, value in snapshot.items() if key != "content_hash"}
        snapshot["content_hash"] = canonical_hash(unsigned)
        self.seed(run_id, "INTAKE", None, snapshot)
        action = self.action_for(
            run_id, "SPEC", "GRILL", specialized_examples()["decision-log"]
        )
        valid = specialized_examples()["spec"]
        valid["behaviors"].append({"id": "B-2", "number": 2, "description": "Also resolve"})
        valid["acceptance_scenarios"].append({
            "id": "S-2", "acceptance_point_ids": ["AC-2"],
            "given": "another query", "when": "resolved", "then": "another answer",
        })
        valid["traceability"].append({
            "acceptance_point_id": "AC-2", "behavior_ids": ["B-2"], "scenario_ids": ["S-2"],
        })
        incomplete = specialized_examples()["spec"]
        unknown = copy.deepcopy(valid)
        unknown["acceptance_scenarios"][1]["acceptance_point_ids"] = ["AC-X"]
        unknown["traceability"][1]["acceptance_point_id"] = "AC-X"

        accepted = self.validate_draft(action, valid)
        omitted = self.validate_draft(action, incomplete)
        unrelated = self.validate_draft(action, unknown)

        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(omitted["reason_code"], "TRACEABILITY_MISMATCH")
        self.assertEqual(unrelated["reason_code"], "TRACEABILITY_MISMATCH")

    def spec_run(self, run_id, acceptance, acceptance_delta):
        snapshot = specialized_examples()["requirement-snapshot"]
        snapshot["acceptance"] = acceptance
        unsigned = {key: value for key, value in snapshot.items() if key != "content_hash"}
        snapshot["content_hash"] = canonical_hash(unsigned)
        self.seed(run_id, "INTAKE", None, snapshot)
        grill = specialized_examples()["decision-log"]
        grill["acceptance_delta"] = acceptance_delta
        return self.action_for(run_id, "SPEC", "GRILL", grill)

    def test_grill_acceptance_delta_supplies_criteria_for_a_bare_card(self):
        action = self.spec_run("run-bind-spec-bare", [], [{
            "id": "AC-1", "statement": "Resolver answers within the budget",
            "decided_by": "owner@baidu.com", "evidence": ["icafe://BGW-1#comment-1"],
        }])

        result = self.validate_draft(action, specialized_examples()["spec"])

        self.assertTrue(result["ok"], result)

    def test_spec_stops_when_no_acceptance_criterion_exists_anywhere(self):
        action = self.spec_run("run-bind-spec-empty", [], [])

        result = self.validate_draft(action, specialized_examples()["spec"])

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "ACCEPTANCE_CRITERIA_MISSING")

    def test_acceptance_delta_may_not_redefine_a_snapshot_criterion(self):
        action = self.spec_run("run-bind-spec-conflict", ["AC-1"], [{
            "id": "AC-1", "statement": "Restated by grill",
            "decided_by": "owner@baidu.com", "evidence": ["icafe://BGW-1#comment-1"],
        }])

        result = self.validate_draft(action, specialized_examples()["spec"])

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "ACCEPTANCE_DELTA_CONFLICT")

    def test_task_dag_coverage_is_bound_to_predecessor_spec(self):
        run_id = "run-bind-dag"
        action = self.action_for(run_id, "TASKS", "SPEC", specialized_examples()["spec"])
        unrelated = specialized_examples()["task-dag"]
        unrelated["nodes"][0]["acceptance_point_ids"] = ["AP-X"]
        unrelated["acceptance_coverage"][0]["acceptance_point_id"] = "AP-X"

        result = self.validate_draft(action, unrelated)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "TRACEABILITY_MISMATCH")

    def test_task_plan_binds_exact_dag_node_revisions_and_g4_action(self):
        run_id = "run-bind-plan"
        revisions = {"business": "r1", "tests": "t1"}
        action = self.action_for(
            run_id, "PLAN", "TASKS", specialized_examples()["task-dag"],
            task_id="T-1", revisions=revisions,
        )
        valid = specialized_examples()["task-plan"]
        valid["g4_input_hash"] = action["input_hash"]
        self.assertTrue(self.validate_draft(action, valid)["ok"])

        cases = []
        wrong_gate = copy.deepcopy(valid)
        wrong_gate["g4_input_hash"] = "0" * 64
        cases.append((wrong_gate, "G4_INPUT_MISMATCH"))
        wrong_revision = copy.deepcopy(valid)
        wrong_revision["repositories"][0]["revision"] = "other"
        cases.append((wrong_revision, "SOURCE_REVISION_MISMATCH"))
        wrong_node = copy.deepcopy(valid)
        wrong_node["tests"][0]["test_id"] = "other.test"
        cases.append((wrong_node, "TASK_PLAN_MISMATCH"))
        for content, reason in cases:
            with self.subTest(reason=reason):
                result = self.validate_draft(action, content)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason_code"], reason)

    def test_change_set_binds_plan_baseline_revisions_and_recomputed_hashes(self):
        run_id = "run-bind-change"
        baselines = {"business": "r1", "tests": "t1"}
        candidates = {"business": "r2", "tests": "t2"}
        plan = specialized_examples()["task-plan"]
        action = self.action_for(
            run_id, "IMPLEMENT", "PLAN", plan, task_id="T-1", revisions=baselines,
        )
        valid = specialized_examples()["change-set"]
        self.assertTrue(self.validate_draft(
            action, valid, source_revisions=candidates
        )["ok"])

        wrong_baseline = specialized_examples()["change-set"]
        wrong_baseline["baseline_revisions"]["business"] = "other"
        wrong_baseline["candidate_hash"] = canonical_hash({
            key: value for key, value in wrong_baseline.items() if key != "candidate_hash"
        })
        wrong_revisions = specialized_examples()["change-set"]
        wrong_revisions["revisions"]["tests"] = "other"
        wrong_revisions["candidate_hash"] = canonical_hash({
            key: value for key, value in wrong_revisions.items() if key != "candidate_hash"
        })
        for content, reason in (
            (wrong_baseline, "BASELINE_REVISION_MISMATCH"),
            (wrong_revisions, "SOURCE_REVISION_MISMATCH"),
        ):
            with self.subTest(reason=reason):
                result = self.validate_draft(action, content, source_revisions=candidates)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason_code"], reason)

    def test_natural_plan_to_implement_flow_separates_baseline_and_candidate_revisions(self):
        run_id = "run-natural-candidate"
        baselines = {"business": "r1", "tests": "t1"}
        candidates = {"business": "r2", "tests": "t2"}
        self.seed(run_id, "TASKS", None, specialized_examples()["task-dag"])
        self.state.transition(run_id, "PLAN", {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64,
            "task_id": "T-1", "source_revisions": baselines,
        })
        plan_action = self.protocol.next(run_id)
        plan = specialized_examples()["task-plan"]
        plan["g4_input_hash"] = plan_action["input_hash"]
        plan_draft = self.draft(plan_action, plan, approval_id="approval-natural-plan")
        self.approvals.approve(
            approval_id="approval-natural-plan", run_id=run_id, gate="G4",
            input_hash=plan_draft["approval_input_hash"],
        )
        planned = self.protocol.complete(run_id, plan_draft)
        implement_action = self.protocol.next(run_id)
        change = specialized_examples()["change-set"]
        candidate_draft = self.draft(
            implement_action, change, approval_id="approval-natural-implement",
            source_revisions=candidates,
        )
        self.approvals.approve(
            approval_id="approval-natural-implement", run_id=run_id, gate="G5",
            input_hash=candidate_draft["approval_input_hash"],
        )
        undeclared = copy.deepcopy(candidate_draft)
        undeclared["source_revisions"]["tests"] = "unrelated"

        rejected = self.protocol.validate_result(implement_action, undeclared)
        implemented = self.protocol.complete(run_id, candidate_draft)
        review_action = self.protocol.next(run_id)

        self.assertTrue(planned["ok"], planned)
        self.assertEqual(implement_action.get("baseline_revisions"), baselines)
        self.assertEqual(implement_action["source_revisions"], baselines)
        self.assertEqual(rejected["reason_code"], "SOURCE_REVISION_MISMATCH")
        self.assertTrue(implemented["ok"], implemented)
        self.assertEqual(review_action["source_revisions"], candidates)

    def test_review_and_diagnosis_bind_their_exact_predecessor(self):
        revisions = {"business": "r2", "tests": "t2"}
        review_action = self.action_for(
            "run-bind-review", "REVIEW", "IMPLEMENT", specialized_examples()["change-set"],
            task_id="T-1", revisions=revisions,
        )
        wrong_review = specialized_examples()["review"]
        wrong_review["change_set_hash"] = "0" * 64
        review_result = self.validate_draft(review_action, wrong_review)

        diagnose_action = self.action_for(
            "run-bind-diagnose", "DIAGNOSE", "IMPLEMENT", specialized_examples()["change-set"],
            task_id="T-1", revisions=revisions,
        )
        wrong_diagnosis = specialized_examples()["diagnosis"]
        wrong_diagnosis["frozen_revisions"]["tests"] = "other"
        diagnosis_result = self.validate_draft(diagnose_action, wrong_diagnosis)

        self.assertEqual(review_result["reason_code"], "REVIEW_PREDECESSOR_MISMATCH")
        self.assertEqual(diagnosis_result["reason_code"], "SOURCE_REVISION_MISMATCH")


if __name__ == "__main__":
    unittest.main()


class CodeOnlyRepairRoutesToPlan(unittest.TestCase):
    """A code-only repair must not drag the Spec and the DAG through their gates again."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.protocol = PhaseProtocol(
            state_store=StateStore(root / "state.sqlite"),
            artifact_store=ArtifactStore(root / "artifacts"),
            knowledge_sync=ProductionShapeKnowledgeSync(),
            approval_ledger=BoundApprovalLedger(),
            evidence_gate=EvidenceGate(),
            transition_policy=TransitionPolicy(),
        )

    def _diagnosis(self, scope=None):
        diagnosis = specialized_examples()["diagnosis"]
        diagnosis["route"] = "REPAIR"
        if scope is not None:
            diagnosis["repair_scope"] = scope
        return diagnosis

    def test_code_only_repair_reenters_at_plan_and_keeps_the_task(self):
        action = {"phase": "DIAGNOSE", "task_id": "T-1", "run_id": "run-scope"}
        target = self.protocol._completion_target(
            action, {"content": self._diagnosis("CODE_ONLY")}
        )

        self.assertEqual(target, ("PLAN", "T-1", "OK"))

    def test_spec_amendment_and_missing_scope_both_reenter_at_spec(self):
        action = {"phase": "DIAGNOSE", "task_id": "T-1", "run_id": "run-scope"}
        amended = self.protocol._completion_target(
            action, {"content": self._diagnosis("SPEC_AMENDMENT")}
        )
        silent = self.protocol._completion_target(action, {"content": self._diagnosis()})

        self.assertEqual(amended, ("SPEC", None, "OK"))
        self.assertEqual(silent, ("SPEC", None, "OK"))

    def test_policy_allows_the_plan_reentry_edge(self):
        allowed = self.protocol.transitions.validate("DIAGNOSE", "PLAN")
        still_allowed = self.protocol.transitions.validate("DIAGNOSE", "SPEC")

        self.assertTrue(allowed["allowed"], allowed)
        self.assertTrue(still_allowed["allowed"], still_allowed)
