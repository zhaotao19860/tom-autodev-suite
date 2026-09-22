"""Diagnosis proposes a repair before a patch exists and preserves its actual source."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from schema_validator import validate_named_schema
from test_phase_protocol import FakeKnowledgeSync
from test_phase_protocol_repair import final_envelope
from test_schema_validation import specialized_examples
import worker_driver
import workflow_spec


class DiagnosisContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.orch = Orchestrator(self.root)
        self.sync = FakeKnowledgeSync()
        self.run_id = "diagnosis-contract"
        self.revisions = {"business": "r2", "tests": "t2"}
        self.seed("TASKS", None, specialized_examples()["task-dag"])

    def seed(self, phase, task, content):
        return self.orch.artifacts.put_envelope(final_envelope(
            self.run_id, phase, task, content, revisions=self.revisions))

    def enter(self, phase, **payload):
        self.orch.state.transition(self.run_id, phase, {
            "requirement_id": "BGW-1", "profile_hash": "a" * 64,
            "workflow_spec_hash": workflow_spec.canonical_hash(),
            "workflow_modes": workflow_spec.run_workflow_modes("full"),
            "source_revisions": self.revisions, "task_id": "T-1", **payload,
        })

    def job(self):
        result = worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync)
        self.assertEqual(result.get("parked"), worker_driver.PRODUCER_WAIT, result)
        return result["producer_job"]["job_id"]

    def submit(self, job_id, content):
        return worker_driver.submit_draft(
            self.orch, self.run_id, job_id, content, knowledge_sync=self.sync)

    def approve(self, gate, input_hash):
        ledger = self.orch.approvals
        record = ledger.request(gate, input_hash, ["comate", "infoflow"], run_id=self.run_id,
                                member_policy={c: ["owner@example.test"] for c in ("comate", "infoflow")})
        for channel in ("comate", "infoflow"):
            ledger.record_delivery(record["approval_id"], channel, {"request_id": channel},
                                   payload_hash=input_hash)
        ledger.resolve(record["approval_id"], "APPROVE", input_hash, "comate",
                       run_id=self.run_id, responder="owner@example.test", state_store=self.orch.state)

    def pipeline_failure(self):
        failure = specialized_examples()["ipipe-evidence"]
        failure.update(status="FAILURE", classification="CODE", failure_signature="SIG-1")
        failure["stages"][0]["status"] = "FAILURE"
        failure["jobs"][0]["status"] = "FAILURE"
        stored = self.seed("IPIPE", None, failure)
        self.enter("DIAGNOSE", previous_state="IPIPE", artifact_id=stored["artifact_id"])
        proposal = specialized_examples()["diagnosis"]
        proposal.update(repair_diff_hash=None, repair_scope="CODE_ONLY", stage_id="unit")
        return proposal

    def review_failure(self):
        change = specialized_examples()["change-set"]
        self.seed("IMPLEMENT", "T-1", change)
        self.enter("REVIEW")
        review = specialized_examples()["review"]
        review.update(verdict="REJECT", change_set_hash=change["candidate_hash"])
        review["findings"] = [{
            "id": "F-1", "axis": "spec", "severity": "P1", "location": "src/resolver.cc:8",
            "evidence": "null query reaches dereference", "acceptance_point_ids": ["AC-1"],
            "blocking": True, "classification": "CONFIRMED",
        }]
        review["axes"]["spec"]["finding_ids"] = ["F-1"]
        rejected = self.submit(self.job(), review)
        self.assertEqual(rejected.get("state"), "DIAGNOSE", rejected)
        proposal = specialized_examples()["diagnosis"]
        proposal.update(repair_diff_hash=None, repair_scope="CODE_ONLY", build_id=None,
                        stage_id=None, job_id=None, environment_fingerprint=None,
                        log_evidence=["git:src/resolver.cc@r2"],
                        reproduction=["source trace of null query"],
                        comparison=["passing branch guards null input"])
        return proposal

    def finish_diagnosis(self, proposal):
        job_id = self.job()
        waiting = self.submit(job_id, proposal)
        self.assertEqual(waiting.get("reason_code"), "APPROVAL_REQUIRED", waiting)
        self.assertEqual(waiting["gate"], "G6")
        self.assertEqual(self.orch.state.events(self.run_id)[-1]["state"], "DIAGNOSE")
        self.assertEqual(self.orch.artifacts.phase_artifacts(self.run_id, "PLAN"), [])
        self.approve("G6", waiting["approval_input_hash"])
        # A new controller consumes the stored, approved proposal, without model resubmission.
        self.orch = Orchestrator(self.root)
        worker_driver.advance(self.orch, self.run_id, knowledge_sync=self.sync, max_steps=1)
        self.assertEqual(self.orch.state.events(self.run_id)[-1]["state"], "PLAN")
        action = self.orch.next(self.run_id)
        self.assertEqual((action["task_id"], action["required_human_gate"]), ("T-1", "G4"))
        saved = self.orch.artifacts.latest_phase(self.run_id, "DIAGNOSE")
        self.assertIsNone(saved["envelope"]["content"]["repair_diff_hash"])

    def test_patchless_pipeline_repair_proposal_resumes_to_plan_after_g6(self):
        self.finish_diagnosis(self.pipeline_failure())

    def test_rejected_source_review_can_be_diagnosed_without_fake_pipeline_ids(self):
        self.finish_diagnosis(self.review_failure())

    def test_source_diagnosis_cannot_claim_unrelated_pipeline_execution(self):
        proposal = self.review_failure()
        proposal.update(build_id="unrelated-build", stage_id="unit", job_id="job-1",
                        environment_fingerprint="env-1")
        result = self.submit(self.job(), proposal)
        self.assertEqual(result.get("reason_code"), "DIAGNOSIS_SOURCE_MISMATCH", result)
        self.assertTrue(result.get("retry_allowed"), result)

    def test_pipeline_diagnosis_rejects_erased_or_mismatched_failure_identity(self):
        proposal = self.pipeline_failure()
        job_id = self.job()
        for field, bad in [("build_id", None), ("environment_fingerprint", "other-env"),
                           ("stage_id", "other-stage"), ("job_id", "other-job"),
                           ("failure_signature", "different-signature")]:
            with self.subTest(field=field):
                result = self.submit(job_id, {**proposal, field: bad})
                self.assertEqual(result.get("reason_code"), "DIAGNOSIS_SOURCE_MISMATCH", result)
                self.assertEqual(self.orch.state.producer_job(job_id)["status"], "PENDING")

    def test_sufficient_proposal_needs_cause_and_plan_even_without_diff(self):
        proposal = specialized_examples()["diagnosis"]
        proposal["repair_diff_hash"] = None
        self.assertEqual(validate_named_schema(proposal, "diagnosis"), [])
        for field, bad in [("hypothesis", None), ("repair_plan", [])]:
            self.assertTrue(validate_named_schema({**proposal, field: bad}, "diagnosis"))


if __name__ == "__main__":
    unittest.main()
