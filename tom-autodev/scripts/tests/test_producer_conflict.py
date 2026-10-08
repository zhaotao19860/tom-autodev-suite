import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema_validator import validate_named_schema
import worker_driver
import workflow_spec


class ProducerConflictTests(unittest.TestCase):
    def report(self):
        return {
            "run_id": "run-1",
            "job_id": "producer:plan",
            "action_id": "plan",
            "source_event_id": "event",
            "task_id": "T1",
            "input_hash": "hash",
            "predecessor_artifact_id": "tasks",
            "predecessor_content_hash": "a" * 64,
            "source_revisions": {"business": "b", "tests": "t"},
            "reason": "已批准 Spec 与当前需求决策冲突",
            "evidence": ["spec:B2", "tasks:T1"],
            "classification": "SPEC_CONFLICT",
            "workspace_state": "NO_CODE_CHANGES",
        }

    def test_schema_requires_no_code_changes(self):
        report = self.report()
        self.assertEqual(validate_named_schema(report, "producer-conflict"), [])
        self.assertTrue(validate_named_schema({**report, "workspace_state": "DIRTY"}, "producer-conflict"))

    def test_plan_spec_conflict_routes_to_diagnose(self):
        self.assertIn("DIAGNOSE", workflow_spec.allowed_transitions()["PLAN"])
        self.assertEqual(workflow_spec.failure_target("SPEC_CONFLICT"), "DIAGNOSE")

    def test_report_job_id_must_match_reported_job(self):
        report = self.report()
        action = {key: report[key] for key in (
            "run_id", "action_id", "source_event_id", "task_id", "input_hash", "source_revisions"
        )}
        action["phase"] = "PLAN"
        orchestrator = Mock()
        orchestrator.state.producer_job.return_value = {"status": "PENDING"}
        decision = {"kind": worker_driver.PRODUCER_WAIT, "action": action}
        with patch.object(worker_driver, "classify_next", return_value=decision):
            result = worker_driver.report_producer_conflict(
                orchestrator, "run-1", "producer:plan", {**report, "job_id": "producer:other"}
            )
        self.assertEqual(result["reason_code"], "PRODUCER_CONFLICT_BINDING_MISMATCH")
        orchestrator.artifacts.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
