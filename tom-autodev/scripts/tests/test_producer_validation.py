"""Producer validation must reject before freezing, and recover only invalid drafts."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from schema_validator import validate_named_schema
from test_phase_protocol import FakeKnowledgeSync, _seed_envelope
from test_schema_validation import specialized_examples
import worker_driver
import workflow_spec


def digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


class ProducerValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.orch = Orchestrator(Path(self.temporary.name))
        self.sync = FakeKnowledgeSync()
        self.run_id = "producer-validation"

    def frontier(self, phase="REVIEW", *, merged=False):
        examples = specialized_examples()
        predecessor_phase, predecessor_schema = {
            "REVIEW": ("IMPLEMENT", "change-set"),
            "IMPLEMENT": ("PLAN", "task-plan"),
            "PLAN": ("TASKS", "task-dag"),
            "SPEC": ("GRILL", "decision-log"),
        }[phase]
        if phase == "SPEC":
            self.orch.artifacts.put_envelope(_seed_envelope(
                self.run_id, "INTAKE", None, examples["requirement-snapshot"]))
        predecessor = examples[predecessor_schema]
        self.orch.artifacts.put_envelope(_seed_envelope(
            self.run_id, predecessor_phase,
            "T-1" if phase in {"REVIEW", "IMPLEMENT"} else None, predecessor))
        self.orch.state.transition(self.run_id, phase, {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64,
            "change_class": "standard" if merged else "full",
            "workflow_spec_hash": workflow_spec.canonical_hash(),
            "workflow_modes": workflow_spec.run_workflow_modes("standard" if merged else "full"),
            "source_revisions": {"business": "r1", "tests": "t1"},
            **({"task_id": "T-1"} if phase in {"REVIEW", "PLAN", "IMPLEMENT"} else {}),
        })
        action = self.orch.next(self.run_id)
        self.assertTrue(action["ok"], action)
        draft = examples[action["result_schema"]]
        if phase == "REVIEW":
            draft["change_set_hash"] = predecessor["candidate_hash"]
        elif phase == "PLAN":
            draft["g4_input_hash"] = action["input_hash"]
        if merged:
            draft = {"spec": draft, "dag": examples["task-dag"]}
        parked = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
        return action, parked["producer_job"]["job_id"], copy.deepcopy(draft)

    def submit(self, job_id, draft):
        return worker_driver.submit_draft(
            self.orch, self.run_id, job_id, draft, knowledge_sync=self.sync)

    def approve(self, gate, input_hash):
        record = self.orch.approvals.request(
            gate, input_hash, ["comate", "infoflow"], run_id=self.run_id,
            member_policy={channel: ["owner@example.test"] for channel in ("comate", "infoflow")})
        for channel in ("comate", "infoflow"):
            self.orch.approvals.record_delivery(
                record["approval_id"], channel, {"request_id": f"approval-{channel}"},
                payload_hash=input_hash)
        self.orch.approvals.resolve(
            record["approval_id"], "APPROVE", input_hash, "comate", run_id=self.run_id,
            responder="owner@example.test", state_store=self.orch.state)
        return record["approval_id"]

    def legacy_fulfill(self, action, job_id, draft):
        self.orch.state.fulfill_producer_job(job_id, draft)
        worker_driver._record_model_receipt(
            self.orch, self.run_id, action, draft, [action["result_schema"]])

    def test_wrong_review_task_remains_pending_and_corrected_draft_completes(self):
        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.assertEqual(validate_named_schema(wrong, "review"), [])

        rejected = self.submit(job_id, wrong)

        self.assertEqual(rejected["reason_code"], "TASK_ID_MISMATCH")
        self.assertTrue(rejected.get("retry_allowed"), rejected)
        self.assertEqual(self.orch.state.producer_job(job_id)["status"], "PENDING")
        self.assertEqual(self.orch.state.model_execution_receipts(self.run_id), [])
        self.assertIsNone(worker_driver.cached_draft_for(self.orch, action))
        self.assertTrue(self.submit(job_id, valid)["ok"])

    def test_full_draft_validation_is_pure_and_does_not_require_output_approval(self):
        action, job_id, valid = self.frontier("PLAN")
        protocol = self.orch.phase_protocol(self.sync)
        envelope = worker_driver.build_envelope(action, valid)
        before = self.orch.state.events(self.run_id)

        checked = protocol.validate_draft(action, envelope)

        self.assertTrue(checked["ok"], checked)
        self.assertEqual(protocol.validate_result(action, envelope)["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(self.orch.state.events(self.run_id), before)
        self.assertEqual(self.orch.state.producer_job(job_id)["status"], "PENDING")
        self.assertEqual(self.sync.calls, [])
        for field, value, reason in (
            ("content_hash", "b" * 64, "CONTENT_HASH_MISMATCH"),
            ("evidence_refs", [], "EVIDENCE_REQUIRED"),
            ("task_id", "T-2", "TASK_ID_MISMATCH"),
        ):
            with self.subTest(field=field):
                changed = {**envelope, field: value}
                self.assertEqual(protocol.validate_draft(action, changed)["reason_code"], reason)
                self.assertEqual(protocol.validate_result(action, changed)["reason_code"], reason)

    def test_legacy_wrong_task_is_archived_before_corrected_submit(self):
        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        receipts = self.orch.state.model_execution_receipts(self.run_id)

        result = self.submit(job_id, valid)

        self.assertTrue(result["ok"], result)
        attempts = self.orch.state.producer_job_attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["draft"], wrong)
        self.assertEqual(attempts[0]["draft_hash"], digest(wrong))
        self.assertEqual(attempts[0]["validation"]["reason_code"], "TASK_ID_MISMATCH")
        self.assertEqual(attempts[0]["source_event_id"], action["source_event_id"])
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], valid)
        self.assertEqual(self.orch.state.model_execution_receipts(self.run_id)[0], receipts[0])
        self.assertEqual(worker_driver.cached_draft_for(self.orch, action), valid)

    def test_advance_reopens_legacy_invalid_draft_for_a_new_producer_turn(self):
        action, job_id, valid = self.frontier()
        self.legacy_fulfill(action, job_id, {**valid, "task_id": "T-2"})

        result = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)

        self.assertEqual((result.get("ok"), result.get("parked")), (True, worker_driver.PRODUCER_WAIT), result)
        self.assertEqual(result["producer_job"]["status"], "PENDING")
        self.assertIsNone(worker_driver.cached_draft_for(self.orch, action))
        self.assertEqual(len(self.orch.state.producer_job_attempts(job_id)), 1)
        worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
        self.assertEqual(len(self.orch.state.producer_job_attempts(job_id)), 1)

    def test_valid_approved_draft_cannot_be_replaced(self):
        action, job_id, valid = self.frontier("PLAN")
        waiting = self.submit(job_id, valid)
        self.approve(waiting["gate"], waiting["approval_input_hash"])
        replacement = {**valid, "risks": ["a different risk"]}

        rejected = self.submit(job_id, replacement)

        self.assertEqual(rejected["reason_code"], "PRODUCER_JOB_CONFLICT")
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], valid)
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])
        self.assertTrue(self.submit(job_id, valid)["ok"])

    def test_transient_completion_failure_does_not_reopen_a_valid_draft(self):
        action, job_id, valid = self.frontier()
        self.sync.result = {"ok": False, "reason_code": "KU_UNAVAILABLE"}
        self.assertFalse(self.submit(job_id, valid)["ok"])
        retried = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
        self.assertFalse(retried["ok"])
        self.assertEqual(self.orch.state.producer_job(job_id)["status"], "FULFILLED")
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])
        self.sync.result = None
        self.assertTrue(self.submit(job_id, valid)["ok"])

    def test_merged_legacy_bad_dag_is_archived_and_corrected_bundle_can_retry(self):
        action, job_id, valid = self.frontier("SPEC", merged=True)
        wrong = copy.deepcopy(valid)
        wrong["dag"]["nodes"][0]["acceptance_point_ids"] = ["AC-OTHER"]
        wrong["dag"]["acceptance_coverage"][0]["acceptance_point_id"] = "AC-OTHER"
        self.legacy_fulfill(action, job_id, wrong)

        result = self.submit(job_id, valid)

        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")
        attempts = self.orch.state.producer_job_attempts(job_id)
        self.assertEqual(attempts[0]["draft"], wrong)
        self.assertEqual(attempts[0]["validation"]["reason_code"], "TRACEABILITY_MISMATCH")
        self.approve(result["gate"], result["approval_input_hash"])
        self.assertEqual(self.submit(job_id, valid)["reason_code"], "MERGED_COMPLETE")

    def test_stale_frontier_cannot_reopen_an_old_job(self):
        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        self.orch.state.transition(self.run_id, "REVIEW", {
            "task_id": "T-1", "source_revisions": {"business": "r1", "tests": "t1"}})

        rejected = self.submit(job_id, valid)

        self.assertEqual(rejected["reason_code"], "STALE_PRODUCER_JOB")
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], wrong)
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])

    def test_repaired_gated_draft_requires_a_new_approval_hash(self):
        action, job_id, valid = self.frontier("PLAN")
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        old_hash = worker_driver.build_envelope(action, wrong)["approval_input_hash"]
        self.approve("G4", old_hash)

        result = self.submit(job_id, valid)

        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED")
        self.assertNotEqual(result["approval_input_hash"], old_hash)
        self.assertEqual(self.orch.status(self.run_id)["state"], "PLAN")

    def test_reopen_cas_refuses_a_frontier_that_changed_after_validation(self):
        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        existing = self.orch.state.producer_job(job_id)
        proof = worker_driver._validate_before_fulfill(
            self.orch, self.run_id, wrong, self.sync, action=action)
        self.orch.state.transition(self.run_id, "STOPPED", {"previous_state": "REVIEW"})

        result = self.orch.state.reopen_invalid_producer_job(
            self.run_id, job_id, existing, action, proof)

        self.assertEqual(result["reason_code"], "STALE_PRODUCER_JOB")
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], wrong)

    def test_audit_and_cache_eviction_roll_back_if_reopen_write_fails(self):
        import sqlite3

        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        with sqlite3.connect(self.orch.state.database_path) as connection:
            connection.execute("""CREATE TRIGGER fail_reopen BEFORE UPDATE ON producer_jobs
                WHEN NEW.status = 'PENDING' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected write failure"):
            self.submit(job_id, valid)

        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], wrong)
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])
        self.assertEqual(worker_driver.cached_draft_for(self.orch, action), wrong)

    def test_merged_recovery_does_not_refreeze_a_bad_tail_after_spec_committed(self):
        action, job_id, valid = self.frontier("SPEC", merged=True)
        wrong = copy.deepcopy(valid)
        wrong["dag"]["nodes"][0]["acceptance_point_ids"] = ["AC-OTHER"]
        wrong["dag"]["acceptance_coverage"][0]["acceptance_point_id"] = "AC-OTHER"
        self.legacy_fulfill(action, job_id, wrong)
        envelope = worker_driver.build_envelope(action, valid["spec"])
        envelope["approval_id"] = self.approve("G2", envelope["approval_input_hash"])
        self.assertTrue(self.orch.complete_phase(self.run_id, envelope, knowledge_sync=self.sync)["ok"])

        parked = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)

        self.assertEqual((parked.get("ok"), parked.get("parked")), (True, worker_driver.PRODUCER_WAIT), parked)
        self.assertEqual(parked["producer_job"]["status"], "PENDING")
        self.assertIsNone(parked["producer_job"]["draft"])
        self.assertTrue(self.submit(parked["producer_job"]["job_id"], valid["dag"])["ok"])

    def test_missing_intake_evidence_does_not_authorize_replacing_a_valid_spec(self):
        action, job_id, valid = self.frontier("SPEC", merged=True)
        waiting = self.submit(job_id, valid)
        self.approve(waiting["gate"], waiting["approval_input_hash"])
        intake = self.orch.artifacts.latest_phase(self.run_id, "INTAKE", None)
        Path(intake["path"]).unlink()
        replacement = copy.deepcopy(valid)
        replacement["spec"]["risks"] = ["different risk"]

        rejected = self.submit(job_id, replacement)

        self.assertFalse(rejected["ok"])
        self.assertEqual(self.orch.state.producer_job(job_id)["status"], "FULFILLED")
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], valid)
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])

    def test_plan_rejects_the_wrong_task_before_fulfill_or_approval(self):
        # The PLAN path has a different schema, predecessor, and an output gate.
        action, job_id, valid = self.frontier("PLAN")
        wrong = {**valid, "task_id": "T-2"}
        result = self.submit(job_id, wrong)
        self.assertEqual(result["reason_code"], "TASK_ID_MISMATCH")
        self.assertEqual(self.orch.state.producer_job(job_id)["status"], "PENDING")
        good = self.submit(job_id, valid)
        self.assertEqual(good["reason_code"], "APPROVAL_REQUIRED")

    def test_pending_external_publish_prevents_automatic_reopen(self):
        action, job_id, valid = self.frontier()
        wrong = {**valid, "task_id": "T-2"}
        self.legacy_fulfill(action, job_id, wrong)
        self.orch.state.claim_intent(self.run_id, "knowledge.publish-phase", "pending-publish", {
            "title_hash": "a" * 64, "content_hash": digest(wrong),
        })

        result = self.submit(job_id, valid)

        self.assertEqual(result["reason_code"], "RECOVERY_REQUIRED")
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], wrong)
        self.assertEqual(self.orch.state.producer_job_attempts(job_id), [])

    def test_legacy_invalid_implementation_revisions_are_recoverable_content_errors(self):
        action, job_id, valid = self.frontier("IMPLEMENT")
        wrong = copy.deepcopy(valid)
        wrong["revisions"]["business"] = ""
        wrong["candidate_hash"] = digest({key: value for key, value in wrong.items() if key != "candidate_hash"})
        self.legacy_fulfill(action, job_id, wrong)

        result = self.submit(job_id, valid)

        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED", result)
        self.assertEqual(self.orch.state.producer_job(job_id)["draft"], valid)
        attempts = self.orch.state.producer_job_attempts(job_id)
        self.assertEqual(attempts[0]["validation"]["reason_code"], "SOURCE_REVISION_MISMATCH")


if __name__ == "__main__":
    unittest.main()
