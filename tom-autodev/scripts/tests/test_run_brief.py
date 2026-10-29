"""User-facing status projection tests."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator, main
import run_brief


class BriefFixture:
    def __init__(self, state, action, *, pending=None, approvals=None, card_id="BGW-1",
                 producer_jobs=None, monitoring=None):
        self.state = SimpleNamespace(events=lambda run_id: [
            {"run_id": run_id, "state": state, "created_at": "2026-09-24T00:00:00+00:00",
             "payload": {"requirement_id": card_id, "project": "bgw"}},
        ], pending_intents=lambda run_id: pending or [],
            producer_job=lambda job_id: (producer_jobs or {}).get(job_id),
            ipipe_monitoring=lambda run_id: monitoring or [])
        self.approvals = SimpleNamespace(for_run=lambda run_id: approvals or [])
        self._action = action

    def next(self, run_id, *, read_only=False):
        self.last_read_only = read_only
        return self._action


class RunBriefTests(unittest.TestCase):
    def test_fulfilled_review_is_saved_not_waiting_for_nonexistent_approval(self):
        for gate in (None, "G2"):
            with self.subTest(gate=gate):
                phase = "REVIEW" if gate is None else "SPEC"
                action = {"ok": True, "phase": phase, "action_id": "saved",
                          "child_skill": "tom-review" if gate is None else "tom-spec",
                          "required_human_gate": gate}
                orch = BriefFixture(phase, action, producer_jobs={
                    "producer:saved": {"status": "FULFILLED", "payload": {"phase": phase},
                                       "draft": {}},
                })
                brief = run_brief.build(orch, "run-1")
                expected = "已保存待处理" if gate is None else "已保存待审批"
                self.assertEqual(brief["status_label"], "草案" + expected)
                self.assertIn(expected, run_brief.render(brief))
                if gate is None:
                    self.assertNotIn("待审批", brief["status_label"])
                    self.assertNotIn("审批", brief["next"]["text"])

    def test_status_exposes_latest_ipipe_monitoring_checkpoint(self):
        orch = BriefFixture(
            "IPIPE", {"ok": True, "phase": "IPIPE"},
            monitoring=[{
                "run_id": "run-1", "build_id": "build-1",
                "updated_at": "2026-09-25T00:00:00+00:00",
                "checkpoint": {
                    "status": "MONITORING",
                    "stages": [{"name": "P0新case回归", "status": "RUNNING"}],
                },
            }],
        )
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["ipipe_monitoring"][0]["build_id"], "build-1")
        self.assertIn("P0新case回归", run_brief.render(brief))

    def test_status_evaluates_next_without_recovery_side_effects(self):
        orch = BriefFixture("SUBMIT", {"ok": True, "phase": "SUBMIT"})
        run_brief.build(orch, "run-1")
        self.assertTrue(orch.last_read_only)

    def test_producer_wait_has_human_status_and_agent_next_step(self):
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "child_skill": "tom-spec"})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待生成")
        self.assertEqual(brief["next"]["owner"], "Agent")
        rendered = run_brief.render(brief)
        self.assertIn("待生成", rendered)
        self.assertNotIn("input_hash", rendered)
        self.assertNotIn("event_id", rendered)

    def test_migration_gate_is_named_before_the_phase_gate(self):
        approvals = [
            {"action": "G2#retry-1", "approval_id": "approval-g2", "input_hash": "a" * 64,
             "deadline_at": "2026-09-25T00:00:00+00:00", "created_at": "2026-09-24T00:00:00+00:00"},
            {"action": "WORKFLOW_SPEC_MIGRATION", "approval_id": "approval-mig", "input_hash": "b" * 64,
             "deadline_at": "2026-09-25T01:00:00+00:00", "created_at": "2026-09-24T01:00:00+00:00"},
        ]
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "required_human_gate": None},
                            approvals=approvals)
        brief = run_brief.build(orch, "run-1", include_progress=False)
        self.assertEqual(brief["waiting_on"][0]["approval_id"], "approval-mig")
        self.assertIn("approval-mig", brief["next"]["text"])

    def test_approval_wait_has_human_status_and_approval_action(self):
        approvals = [{"action": "G2", "approval_id": "approval-123456", "input_hash": "a" * 64,
                      "deadline_at": "2026-09-25T00:00:00+00:00", "created_at": "2026-09-24T00:00:00+00:00"}]
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "required_human_gate": "G2"}, approvals=approvals)
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待审批")
        self.assertEqual(brief["next"]["owner"], "你")
        rendered = run_brief.render(brief)
        self.assertIn("如流审批卡", rendered)
        self.assertIn("approval-123456", rendered)
        self.assertNotIn("a" * 64, rendered)

    def test_reissued_approval_hides_older_unresolved_attempt(self):
        approvals = [
            {
                "action": "G1",
                "approval_id": "0fef-old",
                "input_hash": "a" * 64,
                "deadline_at": "2026-09-29T22:37:30+00:00",
                "created_at": "2026-09-29T21:00:00+00:00",
            },
            {
                "action": "G1#retry-1",
                "approval_id": "edc61-new",
                "input_hash": "b" * 64,
                "deadline_at": "2026-09-30T09:40:43+00:00",
                "created_at": "2026-09-30T08:00:00+00:00",
            },
        ]
        orch = BriefFixture(
            "GRILL",
            {"ok": True, "phase": "GRILL", "required_human_gate": "G1"},
            approvals=approvals,
        )
        brief = run_brief.build(orch, "run-1")
        self.assertEqual([item["approval_id"] for item in brief["waiting_on"]], ["edc61-new"])
        self.assertIn("edc61-new", brief["next"]["text"])
        self.assertNotIn("0fef-old", brief["next"]["text"])


    def test_required_gate_without_open_approval_is_not_misreported_as_waiting(self):
        orch = BriefFixture("INTAKE", {"ok": True, "phase": "INTAKE", "required_human_gate": "G0"})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待处理")
        self.assertEqual(brief["next"]["owner"], "Agent")

    def test_approved_candidate_is_ready_for_worker_continue_without_ids_or_hashes(self):
        approved = {"action": "G2", "approval_id": "approval-secret", "input_hash": "b" * 64,
                    "effective_decision": "APPROVE", "created_at": "2026-09-24T00:01:00+00:00",
                    "resolved_at": "2026-09-24T00:02:00+00:00"}
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "required_human_gate": "G2",
                                    "action_id": "a1", "child_skill": "tom-spec"},
                            approvals=[approved], producer_jobs={"producer:a1": {
                                "job_id": "producer:a1", "status": "FULFILLED", "payload": {},
                            }})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "已批准待提交")
        rendered = run_brief.render(brief)
        self.assertIn("continue", rendered)
        self.assertNotIn("补发", rendered)
        self.assertNotIn("approval-secret", rendered)
        self.assertNotIn("b" * 64, rendered)

    def test_external_intent_recovery_is_actionable(self):
        orch = BriefFixture("SUBMIT", {"ok": False, "reason_code": "RECOVERY_REQUIRED"},
                            pending=[{"operation": "icode.submit", "intent_id": "intent-1"}])
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待恢复")
        self.assertEqual(brief["next"]["owner"], "Agent")
        self.assertIn("icode.submit", brief["next"]["text"])

    def test_infoflow_recovery_explains_controlled_redelivery(self):
        orch = BriefFixture(
            "GRILL",
            {"ok": False, "reason_code": "RECOVERY_REQUIRED"},
            pending=[{
                "operation": "approval.delivery.infoflow",
                "intent_id": "intent-1",
                "payload": {"approval_id": "approval-1"},
            }],
        )
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["next"]["owner"], "你")
        self.assertIn("未收到，允许重发", brief["next"]["text"])
        self.assertIn("不会创建新审批", brief["next"]["text"])

    def test_open_gate_does_not_hide_an_unknown_delivery_recovery(self):
        """A gate whose card may never have arrived must not be reported as openable.

        `render` used to overwrite the derived recovery instruction whenever a gate was
        open, sending the operator to a card that was never delivered, and it never
        printed the `intent_id` that `confirm-approval-redelivery` requires.
        """
        approval = {"action": "G1", "approval_id": "approval-1", "input_hash": "a" * 64,
                    "deadline_at": "2026-09-30T09:40:00+00:00", "created_at": "2026-09-29"}
        orch = BriefFixture(
            "GRILL",
            {"ok": False, "reason_code": "RECOVERY_REQUIRED"},
            approvals=[approval],
            pending=[{
                "operation": "approval.delivery.infoflow",
                "intent_id": "intent-9",
                "payload": {"approval_id": "approval-1"},
            }],
        )
        brief = run_brief.build(orch, "run-1")
        rendered = run_brief.render(brief)
        self.assertEqual(brief["next"]["owner"], "你")
        self.assertIn("intent intent-9", brief["next"]["text"])
        self.assertIn("intent intent-9", rendered)
        self.assertNotIn("下一步（你）：打开如流审批卡", rendered)

    def test_unanswered_grill_draft_is_not_reported_as_awaiting_approval(self):
        """An UNRESOLVED draft is FULFILLED but can never open G1.

        Reporting it as "草案已保存待审批" told the operator to call `continue`, which
        only fails again; the way out is answering the questions and resubmitting.
        """
        action = {"ok": True, "phase": "GRILL", "child_skill": "tom-grill",
                  "action_id": "action-7", "required_human_gate": "G1",
                  "result_schema": "decision-log.v1"}
        job = {"job_id": "producer:action-7", "status": "FULFILLED",
               "payload": {"phase": "GRILL", "result_schema": "decision-log.v1"},
               "draft": {"decision_result": "UNRESOLVED",
                         "choice_questions": [{"id": "Q1"}]}}
        orch = BriefFixture("GRILL", action, producer_jobs={job["job_id"]: job})
        brief = run_brief.build(orch, "run-1")
        rendered = run_brief.render(brief)
        self.assertTrue(brief["producer_job"]["awaiting_answers"])
        self.assertEqual(brief["status_label"], "待答题")
        self.assertEqual(brief["next"]["owner"], "你")
        self.assertIn("未答问题", brief["next"]["text"])
        self.assertIn("producer:action-7", rendered)
        self.assertNotIn("补发对应审批", rendered)
        self.assertNotIn("Q1", rendered)

    def test_terminal_state_is_reported_as_complete(self):
        old_approval = {"action": "G2", "approval_id": "old-approval", "input_hash": "a" * 64,
                        "deadline_at": "2026-09-25", "created_at": "2026-09-24"}
        orch = BriefFixture("RELEASE_SUCCESS", {"ok": False, "reason_code": "TERMINAL_STATE"},
                            approvals=[old_approval], pending=[{"operation": "old", "intent_id": "i"}])
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "已完成")
        self.assertIsNone(brief["blocked"])
        self.assertEqual(brief["waiting_on"], [])
        self.assertEqual(brief["in_flight"], [])
        self.assertEqual(brief["next"]["owner"], "-")
        rendered = run_brief.render(brief)
        self.assertIn("已完成", rendered)
        self.assertNotIn("等待审批：G2", rendered)
        self.assertNotIn("TERMINAL_STATE", rendered)

    def test_stopped_state_is_reported_as_ended_without_stale_approval(self):
        approval = {"action": "G2", "approval_id": "stale", "input_hash": "x",
                    "deadline_at": "tomorrow", "created_at": "yesterday"}
        orch = BriefFixture("STOPPED", {"ok": False, "reason_code": "TERMINAL_STATE"}, approvals=[approval])
        brief = run_brief.build(orch, "run-1")
        rendered = run_brief.render(brief)
        self.assertEqual(brief["status_label"], "已停止")
        self.assertIsNone(brief["blocked"])
        self.assertEqual(brief["waiting_on"], [])
        self.assertIn("运行已结束，无后续动作", rendered)
        self.assertNotIn("如流审批卡", rendered)

    def test_status_exposes_pending_producer_job_context_and_saved_draft(self):
        action = {"ok": True, "phase": "SPEC", "child_skill": "tom-spec",
                  "action_id": "action-1", "required_human_gate": "G2",
                  "result_schema": "spec.v1"}
        job = {"job_id": "producer:action-1", "status": "FULFILLED",
               "payload": {"phase": "SPEC", "result_schema": "spec.v1"},
               "draft": {"private": "draft body"}}
        orch = BriefFixture("SPEC", action, producer_jobs={job["job_id"]: job})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["producer_job"], {
            "job_id": "producer:action-1", "phase": "SPEC", "schema": "spec.v1",
            "status": "FULFILLED", "draft_state": "已保存待审批",
            "awaiting_answers": False,
        })
        rendered = run_brief.render(brief)
        self.assertIn("草案已保存待审批", rendered)
        self.assertNotIn("private", rendered)

    def test_card_id_status_cli_resolves_target_and_json_keeps_full_events(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        orch = Orchestrator(root)
        run_id = "0123456789abcdef0123456789abcdef"
        orch.state.transition(run_id, "INTAKE", {"requirement_id": "BGW-1", "project": "bgw"})
        summary = {"run_id": run_id, "state": "INTAKE", "status_label": "待处理",
                   "waiting_on": [], "in_flight": [], "blocked": None,
                   "next": {"owner": "Agent", "text": "继续"}}
        with patch("orchestrator.Orchestrator", return_value=orch), \
             patch("run_brief.build", return_value=summary) as build, \
             patch("run_brief.render", return_value="摘要"):
            output = io.StringIO()
            with patch("sys.stdout", output):
                code = main(["--config-root", str(root), "status", "BGW-1"])
        self.assertEqual((code, output.getvalue().strip()), (0, "摘要"))
        build.assert_called_once_with(orch, run_id)
        detailed = orch.status(run_id)
        with patch("orchestrator.Orchestrator", return_value=orch):
            output = io.StringIO()
            with patch("sys.stdout", output):
                code = main(["--config-root", str(root), "status", run_id, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), detailed)

    def test_ambiguous_card_status_returns_candidates(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        orch = Orchestrator(root)
        for run_id in ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"):
            orch.state.transition(run_id, "INTAKE", {"requirement_id": "BGW-1", "project": "bgw"})
        with patch("orchestrator.Orchestrator", return_value=orch):
            output = io.StringIO()
            with patch("sys.stdout", output):
                code = main(["--config-root", str(root), "status", "BGW-1", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["reason_code"], "AMBIGUOUS_RUN")
        self.assertEqual(len(result["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
