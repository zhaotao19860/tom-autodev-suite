import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema_validator import validate_named_schema
from state_store import StateStore
from artifact_store import ArtifactStore
from orchestrator import Orchestrator
import workflow_spec


class ProducerBlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / "state.sqlite3")

    def report(self):
        return {
            "run_id": "run-1",
            "job_id": "producer:impl-action",
            "action_id": "impl-action",
            "source_event_id": "implement-event",
            "task_id": "T1",
            "input_hash": "implement-input",
            "plan_artifact_id": "plan-artifact",
            "plan_content_hash": "a" * 64,
            "plan_approval_id": "approval-g4",
            "plan_approval_input_hash": "g4-input",
            "source_revisions": {"business": "b1", "tests": "t1"},
            "reason": "用户需求变更取消 MSS 全局严格等值，需重新确认计划边界",
            "evidence": ["approved-plan:plan-artifact", "producer-job:producer:impl-action"],
            "classification": "USER_REQUIREMENT_CHANGED",
            "workspace_state": "NO_CODE_CHANGES",
        }

    def test_report_schema_requires_no_code_changes_and_rejects_extra_fields(self):
        report = self.report()
        self.assertEqual(validate_named_schema(report, "producer-block"), [])
        self.assertTrue(validate_named_schema({**report, "change_set": {}}, "producer-block"))
        self.assertTrue(validate_named_schema({**report, "workspace_state": "DIRTY"}, "producer-block"))
        missing = {key: value for key, value in report.items() if key != "workspace_state"}
        self.assertTrue(validate_named_schema(missing, "producer-block"))

    def test_state_entry_is_idempotent_and_consumed_without_editing_producer_job(self):
        self.store.transition("run-1", "IMPLEMENT", {"task_id": "T1"})
        payload = {
            "phase": "IMPLEMENT", "action_id": "impl-action",
            "source_event_id": "implement-event", "task_id": "T1",
            "input_hash": "implement-input", "result_schema": "change-set",
        }
        self.store.record_producer_job("run-1", "producer:impl-action", payload)
        report = self.report()
        first = self.store.record_producer_block("run-1", "block-1", report["job_id"], report, "artifact-1")
        replay = self.store.record_producer_block("run-1", "block-1", report["job_id"], report, "artifact-1")
        self.assertEqual(first, replay)
        self.assertEqual(self.store.producer_job("producer:impl-action")["status"], "PENDING")
        consumed = self.store.consume_producer_block("run-1", "block-1")
        self.assertEqual(consumed["status"], "CONSUMED")
        self.assertEqual(self.store.consume_producer_block("run-1", "block-1")["status"], "CONSUMED")

    def _pending_block(self):
        self.store.transition("run-1", "IMPLEMENT", {"task_id": "T1"})
        self.store.record_producer_job("run-1", "producer:impl-action", {
            "phase": "IMPLEMENT", "action_id": "impl-action",
            "source_event_id": "implement-event", "task_id": "T1",
            "input_hash": "implement-input", "result_schema": "change-set",
        })
        report = self.report()
        self.store.record_producer_block("run-1", "block-1", report["job_id"], report, "artifact-1")
        return SimpleNamespace(state=self.store, advance=Mock())

    def test_a_later_change_set_supersedes_a_pending_block(self):
        import worker_driver

        orchestrator = self._pending_block()
        self.store.fulfill_producer_job("producer:impl-action", {"summary": "real change set"})
        decision = {"action": {"phase": "IMPLEMENT", "action_id": "impl-action",
                               "source_event_id": "implement-event"}}
        self.assertIsNone(worker_driver._consume_producer_block(orchestrator, "run-1", decision))
        orchestrator.advance.assert_not_called()

    def test_change_set_is_refused_while_its_block_is_pending(self):
        import worker_driver

        orchestrator = self._pending_block()
        result = worker_driver.submit_draft(orchestrator, "run-1", "producer:impl-action", {})
        self.assertEqual(result["reason_code"], "PRODUCER_BLOCK_PENDING")

    def test_workflow_routes_plan_incomplete_only_from_implement(self):
        self.assertTrue(workflow_spec.allowed_transitions()["IMPLEMENT"].__contains__("DIAGNOSE"))
        self.assertEqual(workflow_spec.failure_target("PLAN_INCOMPLETE"), "DIAGNOSE")
        self.assertEqual(
            workflow_spec.TRANSITION_REQUIREMENTS[("IMPLEMENT", "DIAGNOSE")],
            (("producer-block-report", "task-plan"), None),
        )
        self.assertNotIn("DIAGNOSE", workflow_spec.allowed_transitions()["PLAN"])

    def test_nested_evidence_finds_matching_producer_block_predecessor(self):
        root = Path(self.temp.name) / "orchestrator"
        orchestrator = Orchestrator(root)
        run_id = "run-nested"
        report = self.report()
        report["run_id"] = run_id
        report_artifact = orchestrator.artifacts.put(
            run_id, "producer-block-report",
            json.dumps(report, sort_keys=True).encode(),
            {"report_id": "block-1"},
        )
        orchestrator.state.transition(run_id, "DIAGNOSE", {
            "task_id": "T1",
            "reason_code": "PLAN_INCOMPLETE",
            "evidence": {
                "producer_block_report_id": report_artifact["artifact_id"],
                "reason_code": "PLAN_INCOMPLETE",
                "run_id": run_id,
                "task_id": "T1",
                "source_revisions": report["source_revisions"],
            },
        })
        predecessor = orchestrator.phase_protocol()._predecessor(
            run_id, ("REVIEW", "IPIPE", "IMPLEMENT"), "T1"
        )
        self.assertIsNotNone(predecessor)
        self.assertEqual(predecessor["artifact_id"], report_artifact["artifact_id"])

    def test_nested_evidence_rejects_wrong_identity_or_revisions(self):
        cases = [
            ("run_id", "other-run"),
            ("task_id", "T2"),
            ("source_revisions", {"business": "wrong", "tests": "t1"}),
            ("source_revisions", {"business": "b1"}),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                root = Path(self.temp.name) / f"orchestrator-{field}-{str(value).replace('/', '_')}"
                orchestrator = Orchestrator(root)
                run_id = "run-nested-invalid"
                report = self.report()
                report["run_id"] = run_id
                report_artifact = orchestrator.artifacts.put(
                    run_id, "producer-block-report",
                    json.dumps(report, sort_keys=True).encode(),
                    {"report_id": "block-1"},
                )
                evidence = {
                    "producer_block_report_id": report_artifact["artifact_id"],
                    "reason_code": "PLAN_INCOMPLETE",
                    "run_id": run_id,
                    "task_id": "T1",
                    "source_revisions": report["source_revisions"],
                }
                if field in {"run_id", "task_id"}:
                    evidence[field] = value
                else:
                    evidence[field] = value
                orchestrator.state.transition(run_id, "DIAGNOSE", {
                    "task_id": "T1",
                    "reason_code": "PLAN_INCOMPLETE",
                    "evidence": evidence,
                })
                predecessor = orchestrator.phase_protocol()._predecessor(
                    run_id, ("REVIEW", "IPIPE", "IMPLEMENT"), "T1"
                )
                self.assertIsNone(predecessor)


if __name__ == "__main__":
    unittest.main()
