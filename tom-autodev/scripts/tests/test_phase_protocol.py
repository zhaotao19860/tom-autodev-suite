import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
from evidence_gate import EvidenceGate
from orchestrator import Orchestrator
from state_store import StateStore
from transition_policy import TransitionPolicy
from test_phase_protocol_repair import (
    production_profile,
    record_collaboration_receipt,
    strict_intake_payload,
)
from test_schema_validation import specialized_examples

try:
    from phase_protocol import PhaseProtocol
except ImportError:
    PhaseProtocol = None


HASH = "a" * 64


def canonical_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ipipe_payload(root, artifacts, run_id):
    profile = production_profile()
    profile_bytes = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    profile_path = root / f"{run_id}-profile.yaml"
    profile_path.write_bytes(profile_bytes)
    binding = {
        "pipeline_id": "pipe-1", "module": "resolver",
        "release_rule": "all stages pass",
        "source_revisions": {"business": "r1", "tests": "t1"},
        "environment_fingerprint": canonical_hash(profile["environment_profile"]),
    }
    submission = artifacts.put(
        run_id, "submission", b"submitted", {"controller_binding": copy.deepcopy(binding)}
    )
    return {
        "requirement_id": "BGW-1", "project": "bgw",
        "profile_path": str(profile_path),
        "profile_hash": hashlib.sha256(profile_bytes).hexdigest(),
        **binding,
        "submission_artifact_id": submission["artifact_id"],
        "submission_hash": submission["sha256"],
        "approval_id": "approval-g7", "approval_input_hash": submission["sha256"],
    }


class FakeKnowledgeSync:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def publish_phase(self, run_id, artifact, scope="both"):
        self.calls.append((run_id, artifact))
        if self.result is not None:
            return copy.deepcopy(self.result)
        return {
            "ok": True, "reason_code": "OK", "run_id": run_id,
            "artifact_hash": artifact["content_hash"], "child_doc_id": "doc-1",
            "child_url": "https://ku.baidu-int.com/knowledge/doc-1", "child_version": "v1",
            "index_doc_id": "root-1", "index_version": "v2", "comment_id": "comment-1",
            "evidence_refs": ["ku:doc-1/v1", "ku:root-1/v2", "icafe:BGW-1/comment-1"],
        }


class RaisingKnowledgeSync:
    def publish_phase(self, run_id, artifact, scope="both"):
        raise RuntimeError("transport exploded")


class SettlingKnowledgeSync(FakeKnowledgeSync):
    """A publish that closes its own intents, the way the real one does on success."""

    def __init__(self, state, result=None):
        super().__init__(result)
        self.state = state

    def publish_phase(self, run_id, artifact, scope="both"):
        response = super().publish_phase(run_id, artifact)
        if response.get("ok"):
            for pending in self.state.pending_intents(run_id):
                self.state.receipt(pending["intent_id"], {"ok": True, "reason_code": "OK"}, [])
        return response


class FakeApprovals:
    def __init__(self, action="G0", input_hash=None, decision="APPROVE", run_id="run-1"):
        self.record = {"approval_id": "approval-1", "run_id": run_id, "action": action,
            "input_hash": input_hash, "effective_decision": decision}

    def get(self, approval_id):
        return copy.deepcopy(self.record) if approval_id == "approval-1" else None


class PhaseProtocolTests(unittest.TestCase):
    def setUp(self):
        if PhaseProtocol is None:
            self.fail("PhaseProtocol is not implemented")
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state = StateStore(root / "state.sqlite")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.sync = FakeKnowledgeSync()
        self.profile = root / "bgw-profile.yaml"
        self.profile.write_bytes(b"profile-v1")
        snapshot = specialized_examples()["requirement-snapshot"]
        payload = strict_intake_payload("run-1", snapshot)
        payload["profile_hash"] = hashlib.sha256(b"profile-v1").hexdigest()
        payload["profile_path"] = str(self.profile)
        payload["collaboration_binding"]["profile_hash"] = payload["profile_hash"]
        payload["g0_input_hash"] = canonical_hash({
            key: payload[key]
            for key in (
                "requirement_id", "project", "profile_hash", "requirement_snapshot",
                "collaboration_binding",
            )
        })
        self.state.transition("run-1", "INTAKE", payload)
        record_collaboration_receipt(self.state, "run-1", payload)

    def tearDown(self):
        self.temporary.cleanup()

    def protocol(self, approvals=None, sync=None):
        return PhaseProtocol(
            state_store=self.state, artifact_store=self.artifacts,
            knowledge_sync=sync or self.sync, approval_ledger=approvals,
            evidence_gate=EvidenceGate(), transition_policy=TransitionPolicy(),
        )

    def result_for(self, action, content=None, **changes):
        content = copy.deepcopy(content if content is not None else action.get("content") or specialized_examples()[action["result_schema"]])
        content_hash = canonical_hash(content)
        approval_input_hash = action["input_hash"] if action["phase"] == "INTAKE" else canonical_hash({
            "action_id": action["action_id"], "task_id": action["task_id"],
            "parent_artifact_hash": action["parent_artifact_hash"],
            "source_revisions": action["source_revisions"], "content_hash": content_hash,
        })
        envelope = {
            "action_id": action["action_id"], "source_event_id": action["source_event_id"],
            "host": "comate", "run_id": action["run_id"], "phase": action["phase"],
            "task_id": action["task_id"], "schema_version": "1", "input_hash": action["input_hash"],
            "content_hash": content_hash, "source_revisions": copy.deepcopy(action["source_revisions"]),
            "parent_artifact_hash": action["parent_artifact_hash"], "knowledge_doc_id": None,
            "knowledge_url": None, "knowledge_version": None, "icafe_comment_id": None,
            "evidence_refs": copy.deepcopy(action.get("source_evidence_refs", [])),
            "approval_id": "approval-1", "approval_input_hash": approval_input_hash,
            "content": content,
        }
        envelope.update(changes)
        return envelope

    def test_next_returns_exact_comate_phase_order_and_child_mapping(self):
        action = self.protocol().next("run-1")
        self.assertEqual(
            (action["state"], action["phase"], action["child_skill"], action["result_schema"], action["required_human_gate"], action["host"]),
            ("INTAKE", "INTAKE", None, "requirement-snapshot", "G0", "comate"),
        )
        self.assertEqual(action["parent_artifact_hash"], None)
        self.assertEqual(len(action["input_hash"]), 64)

    def test_next_returns_every_exact_child_mapping_with_a_bound_predecessor(self):
        cases = [
            ("GRILL", "INTAKE", "requirement-snapshot", "tom-grill", "decision-log", "G1"),
            ("SPEC", "GRILL", "decision-log", "tom-spec", "spec", "G2"),
            # Under the default (standard) class TASKS is ungated — the merged design front
            # carries the DAG, so TASKS's own G3 is waived. (The `full` class keeps G3.)
            ("TASKS", "SPEC", "spec", "tom-tasks", "task-dag", None),
            ("PLAN", "TASKS", "task-dag", "tom-plan", "task-plan", "G4"),
            ("IMPLEMENT", "PLAN", "task-plan", "tom-implement", "change-set", "G5"),
            ("REVIEW", "IMPLEMENT", "change-set", "tom-review", "review", None),
            ("DIAGNOSE", "REVIEW", "review", "tom-diagnose", "diagnosis", "G6"),
        ]
        for state, predecessor, predecessor_schema, skill, schema, gate in cases:
            run_id = f"run-map-{state.lower()}"
            task_id = "T-1" if predecessor in {"PLAN", "IMPLEMENT", "REVIEW"} else None
            self.artifacts.put_envelope(
                _seed_envelope(run_id, predecessor, task_id, specialized_examples()[predecessor_schema])
            )
            self.state.transition(run_id, state, {
                "requirement_id": "BGW-1", "profile_hash": HASH,
                "source_revisions": {"business": "r1", "tests": "t1"},
            })
            with self.subTest(state=state):
                action = self.protocol().next(run_id)
                self.assertTrue(action["ok"], action)
                self.assertEqual((action["child_skill"], action["result_schema"], action["required_human_gate"]), (skill, schema, gate))
                self.assertIsNotNone(action["parent_artifact_hash"])

    def test_next_returns_controller_actions_and_terminal_stop_without_child_skill(self):
        predecessors = {
            "WORKSPACE": ("TASKS", "task-dag"),
            "SUBMIT": ("REVIEW", "review"),
            "IPIPE": (None, None),
            "RELEASE": ("IPIPE", "ipipe-evidence"),
        }
        for state, controller in [("WORKSPACE", "workspace"), ("SUBMIT", "submit"), ("IPIPE", "ipipe"), ("RELEASE", "release")]:
            run_id = f"run-{state.lower()}"
            predecessor, schema = predecessors[state]
            if predecessor is not None:
                task_id = "T-1" if predecessor == "REVIEW" else None
                self.artifacts.put_envelope(_seed_envelope(run_id, predecessor, task_id, specialized_examples()[schema]))
            payload = (
                ipipe_payload(Path(self.temporary.name), self.artifacts, run_id)
                if state == "IPIPE"
                else {
                    "requirement_id": "BGW-1", "profile_hash": HASH,
                    "source_revisions": {"business": "r1", "tests": "t1"},
                }
            )
            self.state.transition(run_id, state, payload)
            with self.subTest(state=state):
                action = self.protocol().next(run_id)
                self.assertEqual(action["controller"], controller)
                self.assertIsNone(action["child_skill"])
        self.state.transition("run-terminal", "RELEASE_SUCCESS", {})
        self.assertEqual(self.protocol().next("run-terminal")["reason_code"], "TERMINAL_STATE")

    def test_task_plan_action_uses_one_uncompleted_frontier_task(self):
        self.state.transition("run-plan", "PLAN", {
            "requirement_id": "BGW-1", "profile_hash": HASH,
            "source_revisions": {"business": "r1", "tests": "t1"},
        })
        self.artifacts.put_envelope(_seed_envelope("run-plan", "TASKS", None, specialized_examples()["task-dag"]))
        action = self.protocol().next("run-plan")
        self.assertEqual(action["task_id"], "T-1")
        self.assertEqual(action["child_skill"], "tom-plan")
        self.assertEqual(action["result_schema"], "task-plan")

    def test_next_rejects_pending_intent_and_missing_predecessor(self):
        self.state.intent("run-1", "icode.submit", "pending-1", {"change_set_id": "cs-1"})
        self.assertEqual(self.protocol().next("run-1")["reason_code"], "RECOVERY_REQUIRED")
        self.state.transition("run-spec", "SPEC", {"requirement_id": "BGW-1", "profile_hash": HASH})
        self.assertEqual(self.protocol().next("run-spec")["reason_code"], "PREDECESSOR_REQUIRED")

    def test_next_ignores_a_pending_intent_its_own_publish_redrives(self):
        self.state.intent(
            "run-1", "knowledge.publish-phase", "pending-publish", {"content_hash": "b" * 64}
        )
        self.assertEqual(self.protocol().next("run-1")["reason_code"], "OK")

    def test_validate_rejects_wrong_host_stale_cross_run_and_input_hash(self):
        protocol = self.protocol()
        action = protocol.next("run-1")
        cases = [
            ({"host": "other"}, "HOST_NOT_COMATE"),
            ({"run_id": "run-2"}, "RUN_ID_MISMATCH"),
            ({"source_event_id": "stale"}, "STALE_ACTION"),
            ({"input_hash": "b" * 64}, "INPUT_HASH_MISMATCH"),
        ]
        for mutation, reason in cases:
            result = self.result_for(action, **mutation)
            with self.subTest(reason=reason):
                self.assertEqual(protocol.validate_result(action, result)["reason_code"], reason)

    def test_validate_rejects_a_forged_mutation_of_an_issued_action(self):
        protocol = self.protocol()
        action = protocol.next("run-1")
        forged = {**action, "result_schema": "decision-log"}

        self.assertEqual(
            protocol.validate_result(forged, self.result_for(action))["reason_code"],
            "ACTION_ID_MISMATCH",
        )

    def test_next_rejects_a_changed_pinned_profile(self):
        profile = Path(self.temporary.name) / "profile.yaml"
        profile.write_text("profile-v1", encoding="utf-8")
        run_id = "run-profile"
        snapshot = specialized_examples()["requirement-snapshot"]
        payload = strict_intake_payload(run_id, snapshot)
        payload["profile_path"] = str(profile)
        payload["profile_hash"] = hashlib.sha256(b"profile-v1").hexdigest()
        payload["collaboration_binding"]["profile_hash"] = payload["profile_hash"]
        payload["g0_input_hash"] = canonical_hash({
            key: payload[key]
            for key in (
                "requirement_id", "project", "profile_hash", "requirement_snapshot",
                "collaboration_binding",
            )
        })
        self.state.transition(run_id, "INTAKE", payload)
        record_collaboration_receipt(self.state, run_id, payload)
        self.assertTrue(self.protocol().next(run_id)["ok"])

        profile.write_text("profile-v2", encoding="utf-8")

        self.assertEqual(self.protocol().next(run_id)["reason_code"], "PROFILE_CONFLICT")

    def test_validate_rejects_missing_or_wrong_parent_and_cross_task_content(self):
        action = self.protocol().next("run-1")
        self.assertEqual(self.protocol().validate_result(action, self.result_for(action, parent_artifact_hash=HASH))["reason_code"], "PARENT_ARTIFACT_MISMATCH")
        self.state.transition("run-cross-task", "PLAN", {
            "requirement_id": "BGW-1", "profile_hash": HASH,
            "source_revisions": {"business": "r1", "tests": "t1"},
        })
        self.artifacts.put_envelope(_seed_envelope("run-cross-task", "TASKS", None, specialized_examples()["task-dag"]))
        plan_action = self.protocol().next("run-cross-task")
        cross_task = specialized_examples()["task-plan"]
        cross_task["task_id"] = "T-2"
        self.assertEqual(self.protocol().validate_result(plan_action, self.result_for(plan_action, content=cross_task))["reason_code"], "TASK_ID_MISMATCH")

    def test_validate_rejects_schema_content_hash_and_required_revision_mismatch(self):
        action = self.protocol().next("run-1")
        invalid_content = specialized_examples()["requirement-snapshot"]
        del invalid_content["title"]
        self.assertEqual(self.protocol().validate_result(action, self.result_for(action, content=invalid_content))["reason_code"], "SCHEMA_INVALID")
        bad_hash = self.result_for(action)
        bad_hash["content_hash"] = "0" * 64
        self.assertEqual(self.protocol().validate_result(action, bad_hash)["reason_code"], "CONTENT_HASH_MISMATCH")
        self.state.transition("run-revisions", "PLAN", {
            "requirement_id": "BGW-1", "profile_hash": HASH,
            "source_revisions": {"business": "r1", "tests": "t1"},
        })
        self.artifacts.put_envelope(_seed_envelope("run-revisions", "TASKS", None, specialized_examples()["task-dag"]))
        revision_action = self.protocol().next("run-revisions")
        missing = self.result_for(revision_action, content=specialized_examples()["task-plan"], source_revisions={})
        self.assertEqual(self.protocol().validate_result(revision_action, missing)["reason_code"], "SOURCE_REVISION_MISMATCH")

    def test_validate_rejects_approval_mismatch_and_noncanonical_evidence(self):
        action = self.protocol().next("run-1")
        wrong = FakeApprovals(input_hash="b" * 64)
        self.assertEqual(self.protocol(approvals=wrong).validate_result(action, self.result_for(action))["reason_code"], "APPROVAL_INPUT_MISMATCH")
        bad_refs = self.result_for(action, evidence_refs=["https://signed.example/?token=secret"])
        self.assertEqual(self.protocol().validate_result(action, bad_refs)["reason_code"], "PERSISTENCE_SECRET_REJECTED")

    def test_complete_persists_publishes_verifies_receipts_and_transitions_once(self):
        protocol = self.protocol()
        action = protocol.next("run-1")
        protocol.approvals = FakeApprovals(input_hash=action["input_hash"])
        envelope = self.result_for(action)
        result = protocol.complete("run-1", envelope)
        duplicate = protocol.complete("run-1", envelope)
        self.assertTrue(result["ok"])
        self.assertEqual(result, duplicate)
        self.assertEqual(self.state.events("run-1")[-1]["state"], "GRILL")
        self.assertEqual(len(self.sync.calls), 1)
        self.assertTrue(self.artifacts.latest_phase("run-1", "INTAKE", None)["valid"])

    def test_complete_never_claims_success_for_mismatched_ku_or_icafe_receipt(self):
        action = self.protocol().next("run-1")
        envelope = self.result_for(action)
        cases = [
            ({"ok": True, "artifact_hash": "b" * 64, "child_doc_id": "doc-1", "child_url": "https://ku.baidu-int.com/knowledge/doc-1", "child_version": "v1", "comment_id": "comment-1", "evidence_refs": ["ku:doc-1/v1", "icafe:BGW-1/comment-1"]}, "KU_RECEIPT_MISMATCH"),
            ({"ok": True, "artifact_hash": envelope["content_hash"], "child_doc_id": "doc-1", "child_url": "https://ku.baidu-int.com/knowledge/doc-1", "child_version": "v1", "comment_id": None, "evidence_refs": ["ku:doc-1/v1"]}, "ICAFE_RECEIPT_MISMATCH"),
        ]
        for receipt, reason in cases:
            with self.subTest(reason=reason):
                result = self.protocol(
                    approvals=FakeApprovals(input_hash=action["input_hash"]),
                    sync=FakeKnowledgeSync(receipt),
                ).complete("run-1", envelope)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual(self.state.events("run-1")[-1]["state"], "INTAKE")

    def test_complete_keeps_the_publishers_reason_when_ku_write_fails(self):
        """A failed publish used to come back as KNOWLEDGE_PUBLISH_INCOMPLETE.

        PLAN and IMPLEMENT on BGW-1956 T3 both failed that way: KU had named
        KU_CHILD_CONTENT_UNSETTLED, but complete-phase swallowed it, so the
        operator could not tell a settle-wait from a title conflict.
        """
        action = self.protocol().next("run-1")
        envelope = self.result_for(action)
        result = self.protocol(
            approvals=FakeApprovals(input_hash=action["input_hash"]),
            sync=FakeKnowledgeSync({
                "ok": False,
                "reason_code": "KU_CHILD_CONTENT_UNSETTLED",
                "retry_allowed": True,
            }),
        ).complete("run-1", envelope)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "KU_CHILD_CONTENT_UNSETTLED")
        self.assertTrue(result["retry_allowed"])
        self.assertEqual(self.state.events("run-1")[-1]["state"], "INTAKE")

    def test_complete_returns_recovery_required_for_pending_intent(self):
        action = self.protocol().next("run-1")
        envelope = self.result_for(action)
        self.state.intent("run-1", "icode.submit", "pending-complete", {"change_set_id": "cs-1"})
        result = self.protocol(approvals=FakeApprovals()).complete("run-1", envelope)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "RECOVERY_REQUIRED")

    def test_complete_redrives_a_pending_publish_of_the_same_artifact(self):
        """KU verified its own write late, so the publish intent stayed open.

        The reconciliation lives inside `publish_phase`, so refusing here would leave
        the only way forward a hand-written call to it -- which is what the CDN-URL run
        had to do nine times.
        """
        from phase_protocol import _phase_title

        action = self.protocol().next("run-1")
        envelope = self.result_for(action)
        title = _phase_title(action)
        self.state.intent(
            "run-1",
            "knowledge.publish-phase",
            "pending-same-artifact",
            {
                "title_hash": hashlib.sha256(title.encode("utf-8")).hexdigest(),
                "content_hash": envelope["content_hash"],
            },
        )
        sync = SettlingKnowledgeSync(self.state)
        result = self.protocol(
            approvals=FakeApprovals(input_hash=action["input_hash"]), sync=sync
        ).complete("run-1", envelope)
        self.assertTrue(result["ok"], result)
        self.assertEqual([call[1]["title"] for call in sync.calls], [title])
        self.assertEqual(self.state.pending_intents("run-1"), [])

    def test_complete_still_refuses_a_pending_publish_of_another_artifact(self):
        action = self.protocol().next("run-1")
        envelope = self.result_for(action)
        self.state.intent(
            "run-1",
            "knowledge.publish-phase",
            "pending-other-artifact",
            {
                "title_hash": hashlib.sha256(b"09-something-else").hexdigest(),
                "content_hash": "c" * 64,
            },
        )
        sync = FakeKnowledgeSync()
        result = self.protocol(
            approvals=FakeApprovals(input_hash=action["input_hash"]), sync=sync
        ).complete("run-1", envelope)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "RECOVERY_REQUIRED")
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(sync.calls, [])

    def test_public_completion_converts_dependency_exceptions_to_structured_failure(self):
        protocol = self.protocol(sync=RaisingKnowledgeSync())
        action = protocol.next("run-1")
        protocol.approvals = FakeApprovals(input_hash=action["input_hash"])

        try:
            result = protocol.complete("run-1", self.result_for(action))
        except RuntimeError as error:
            self.fail(f"raw dependency exception escaped: {error}")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "PHASE_COMPLETION_FAILED")


class PhaseTitleTests(unittest.TestCase):
    """A re-entered phase must not reuse the KU title its predecessor already holds."""

    def test_a_second_attempt_at_a_phase_gets_its_own_title(self):
        from phase_protocol import _phase_attempt, _phase_title

        events = [
            {"state": "TASKS", "payload": {"task_id": None}},
            {"state": "WORKSPACE", "payload": {"task_id": "T1"}},
            {"state": "TASKS", "payload": {"task_id": None}},
        ]
        action = {"phase": "TASKS", "task_id": None}

        attempt = _phase_attempt(events, "TASKS", None)

        self.assertEqual(attempt, 2)
        self.assertEqual(_phase_title(action, 1), "03-tasks")
        self.assertEqual(_phase_title(action, attempt), "03-tasks-r2")

    def test_a_second_task_is_not_read_as_a_second_attempt(self):
        from phase_protocol import _phase_attempt, _phase_title

        events = [
            {"state": "PLAN", "payload": {"task_id": "T1"}},
            {"state": "PLAN", "payload": {"task_id": "T2"}},
        ]
        action = {"phase": "PLAN", "task_id": "T2"}

        attempt = _phase_attempt(events, "PLAN", "T2")

        self.assertEqual(attempt, 1)
        self.assertEqual(_phase_title(action, attempt), "04-task-plan/T2")

    def test_a_repair_reentry_into_a_run_scoped_phase_gets_its_own_title(self):
        """A repair re-enters SPEC carrying the diagnosis' task_id.

        The first SPEC entry is run scoped and records task_id None, so counting the
        re-entry per task would return 1 and hand it "02-spec", the title the first
        entry already published. KU refuses that as immutable and the repair loop
        cannot publish its amended Spec at all.
        """
        from phase_protocol import _phase_attempt, _phase_title

        events = [
            {"state": "SPEC", "payload": {"task_id": None}},
            {"state": "REVIEW", "payload": {"task_id": "T1"}},
            {"state": "DIAGNOSE", "payload": {"task_id": "T1"}},
            {"state": "SPEC", "payload": {"task_id": "T1"}},
        ]
        action = {"phase": "SPEC", "task_id": "T1"}

        attempt = _phase_attempt(events, "SPEC", "T1")

        self.assertEqual(attempt, 2)
        self.assertEqual(_phase_title(action, attempt), "02-spec-r2")


class OrchestratorPhaseProtocolTests(unittest.TestCase):
    def test_orchestrator_constructs_protocol_with_shared_control_plane_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(directory)
            sync = FakeKnowledgeSync()
            if not hasattr(orchestrator, "phase_protocol"):
                self.fail("orchestrator PhaseProtocol construction is not implemented")
            protocol = orchestrator.phase_protocol(sync)

        self.assertIs(protocol.state, orchestrator.state)
        self.assertIs(protocol.artifacts, orchestrator.artifacts)
        self.assertIs(protocol.approvals, orchestrator.approvals)
        self.assertIs(protocol.knowledge, sync)


class PhaseArtifactIndexIntegrityTests(unittest.TestCase):
    def test_phase_lookup_rejects_a_tampered_index_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            envelope = _seed_envelope("run-index", "TASKS", None, specialized_examples()["task-dag"])
            stored = store.put_envelope(envelope)
            with store._connect() as connection:
                connection.execute(
                    "UPDATE phase_artifacts SET phase = ? WHERE artifact_id = ?",
                    ("SPEC", stored["artifact_id"]),
                )

            loaded = store.latest_phase("run-index", "SPEC", None)

        self.assertFalse(loaded["valid"])
        self.assertEqual(loaded["reason_code"], "ARTIFACT_INDEX_MISMATCH")

    def test_v1_review_and_change_set_without_later_fields_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            review = copy.deepcopy(specialized_examples()["review"])
            for finding in review["findings"]:
                finding.pop("classification", None)
                finding.pop("disposition_reason", None)
            change_set = copy.deepcopy(specialized_examples()["change-set"])
            change_set.pop("deviations", None)
            change_set["candidate_hash"] = canonical_hash({
                key: value for key, value in change_set.items() if key != "candidate_hash"
            })

            review_id = store.put_envelope(_seed_envelope("legacy-review", "REVIEW", "T-1", review))
            change_id = store.put_envelope(_seed_envelope("legacy-change", "IMPLEMENT", "T-1", change_set))

            self.assertTrue(store.phase_artifact(review_id["artifact_id"])["valid"])
            self.assertTrue(store.phase_artifact(change_id["artifact_id"])["valid"])


class IpipeEvidenceAggregationTests(unittest.TestCase):
    def test_required_module_success_is_read_from_phase_envelopes_and_bound_to_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite")
            artifacts = ArtifactStore(root / "artifacts")
            profile = production_profile()
            profile["business_repos"] = [
                {"path": "/repo/x86", "module": "x86bgw", "branch": "main", "lock": "x86-main"},
                {"path": "/repo/agent", "module": "bgwagent", "branch": "main", "lock": "agent-main"},
            ]
            profile["pipeline_profile"]["pipelines"] = [
                {"module": "x86bgw", "pipeline_id": "pipe-x86", "stage_classes": ["unit"], "release_rule": "x86-rule", "required_for_release": True},
                {"module": "bgwagent", "pipeline_id": "pipe-agent", "stage_classes": ["unit"], "release_rule": "agent-rule", "required_for_release": True},
            ]
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            run_id = "run-ipipe-aggregate"
            bindings = {}
            submissions = []
            for module, revision, pipeline_id in (
                ("x86bgw", "x86-rev", "pipe-x86"),
                ("bgwagent", "agent-rev", "pipe-agent"),
            ):
                binding = {
                    "pipeline_id": pipeline_id, "module": module,
                    "release_rule": "x86-rule" if module == "x86bgw" else "agent-rule",
                    "source_revisions": {"business": revision, "tests": "tests-rev"},
                    "environment_fingerprint": canonical_hash(profile["environment_profile"]),
                }
                stored = artifacts.put(run_id, "submission", module.encode(), {"controller_binding": binding})
                submissions.append({
                    "artifact_id": stored["artifact_id"], "sha256": stored["sha256"],
                    "controller_binding": binding,
                })
                bindings[module] = (stored, binding)
            payload = {
                "profile_path": str(profile_path), "submissions": submissions,
                "module": "x86bgw", "pipeline_id": "pipe-x86",
                "source_revisions": bindings["x86bgw"][1]["source_revisions"],
                "submission_artifact_id": bindings["x86bgw"][0]["artifact_id"],
                "submission_hash": bindings["x86bgw"][0]["sha256"],
            }
            state.transition(run_id, "IPIPE", payload)
            previous = copy.deepcopy(specialized_examples()["ipipe-evidence"])
            previous.update({
                "pipeline_id": "pipe-x86", "build_id": "build-x86", "module": "x86bgw",
                "release_rule": "x86-rule",
                "revisions": bindings["x86bgw"][1]["source_revisions"],
                "environment_fingerprint": bindings["x86bgw"][1]["environment_fingerprint"],
            })
            previous_envelope = _seed_envelope(run_id, "IPIPE", None, previous)
            previous_envelope["parent_artifact_hash"] = bindings["x86bgw"][0]["sha256"]
            artifacts.put_envelope(previous_envelope)
            current = copy.deepcopy(specialized_examples()["ipipe-evidence"])
            current.update({
                "pipeline_id": "pipe-agent", "build_id": "build-agent", "module": "bgwagent",
                "release_rule": "agent-rule",
                "revisions": bindings["bgwagent"][1]["source_revisions"],
                "environment_fingerprint": bindings["bgwagent"][1]["environment_fingerprint"],
            })
            protocol = PhaseProtocol(
                state_store=state, artifact_store=artifacts,
                knowledge_sync=FakeKnowledgeSync(), evidence_gate=EvidenceGate(),
                transition_policy=TransitionPolicy(), approval_ledger=None,
            )

            self.assertEqual(protocol._ipipe_outstanding_modules(state.events(run_id), current), [])

    def test_success_from_an_old_revision_does_not_satisfy_the_current_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite")
            artifacts = ArtifactStore(root / "artifacts")
            profile = production_profile()
            profile["business_repos"].append(
                {"path": "/repo/agent", "module": "agent", "branch": "main", "lock": "agent-main"}
            )
            profile["pipeline_profile"]["pipelines"] = [
                {"module": "resolver", "pipeline_id": "pipe-1", "stage_classes": ["unit"], "required_for_release": True},
                {"module": "agent", "pipeline_id": "pipe-agent", "stage_classes": ["unit"], "required_for_release": True},
            ]
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            run_id = "run-ipipe-stale"
            current_binding = {
                "pipeline_id": "pipe-1", "module": "resolver", "release_rule": "all stages pass",
                "source_revisions": {"business": "new", "tests": "tests-new"},
                "environment_fingerprint": canonical_hash(profile["environment_profile"]),
            }
            stored = artifacts.put(run_id, "submission", b"current", {"controller_binding": current_binding})
            agent_binding = {
                "pipeline_id": "pipe-agent", "module": "agent", "release_rule": "all stages pass",
                "source_revisions": {"business": "agent-new", "tests": "tests-new"},
                "environment_fingerprint": current_binding["environment_fingerprint"],
            }
            agent_submission = artifacts.put(run_id, "submission", b"agent-current", {"controller_binding": agent_binding})
            old = copy.deepcopy(specialized_examples()["ipipe-evidence"])
            old.update({"revisions": {"business": "old", "tests": "tests-old"}, "environment_fingerprint": current_binding["environment_fingerprint"]})
            envelope = _seed_envelope(run_id, "IPIPE", None, old)
            envelope["parent_artifact_hash"] = stored["sha256"]
            artifacts.put_envelope(envelope)
            payload = {
                "profile_path": str(profile_path), "module": "resolver", "pipeline_id": "pipe-1",
                "source_revisions": current_binding["source_revisions"],
                "submission_artifact_id": stored["artifact_id"], "submission_hash": stored["sha256"],
                "submissions": [{
                    "artifact_id": stored["artifact_id"], "sha256": stored["sha256"],
                    "controller_binding": current_binding,
                }, {
                    "artifact_id": agent_submission["artifact_id"], "sha256": agent_submission["sha256"],
                    "controller_binding": agent_binding,
                }],
            }
            state.transition(run_id, "IPIPE", payload)
            current = copy.deepcopy(specialized_examples()["ipipe-evidence"])
            current.update({
                "module": "agent", "pipeline_id": "pipe-agent",
                "revisions": agent_binding["source_revisions"],
                "environment_fingerprint": agent_binding["environment_fingerprint"],
            })
            protocol = PhaseProtocol(
                state_store=state, artifact_store=artifacts,
                knowledge_sync=FakeKnowledgeSync(), evidence_gate=EvidenceGate(),
                transition_policy=TransitionPolicy(), approval_ledger=None,
            )

            self.assertEqual(protocol._ipipe_outstanding_modules(state.events(run_id), current), ["resolver"])


def _seed_envelope(run_id, phase, task_id, content):
    envelope = {
        "action_id": canonical_hash({"run_id": run_id, "phase": phase, "task_id": task_id}),
        "source_event_id": "seed", "host": "comate", "run_id": run_id,
        "phase": phase, "task_id": task_id, "schema_version": "1", "input_hash": HASH,
        "content_hash": canonical_hash(content),
        "source_revisions": ({"business": "r1", "tests": "t1"} if phase in {"PLAN", "IMPLEMENT", "REVIEW", "DIAGNOSE", "IPIPE"} else {}),
        "parent_artifact_hash": None,
        "knowledge_doc_id": "doc-seed", "knowledge_url": "https://ku.baidu-int.com/knowledge/doc-seed",
        "knowledge_version": "v1", "icafe_comment_id": "comment-seed",
        "evidence_refs": ["ku:doc-seed/v1", "icafe:BGW-1/comment-seed"],
        "approval_id": None, "approval_input_hash": None,
        "content": copy.deepcopy(content),
    }
    return envelope


if __name__ == "__main__":
    unittest.main()
