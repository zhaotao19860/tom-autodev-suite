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
    def __init__(self, state, action, *, pending=None, approvals=None, card_id="BGW-1"):
        self.state = SimpleNamespace(events=lambda run_id: [
            {"run_id": run_id, "state": state, "created_at": "2026-09-24T00:00:00+00:00",
             "payload": {"requirement_id": card_id, "project": "bgw"}},
        ], pending_intents=lambda run_id: pending or [])
        self.approvals = SimpleNamespace(for_run=lambda run_id: approvals or [])
        self._action = action

    def next(self, run_id):
        return self._action


class RunBriefTests(unittest.TestCase):
    def test_producer_wait_has_human_status_and_agent_next_step(self):
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "child_skill": "tom-spec"})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待生成")
        self.assertEqual(brief["next"]["owner"], "Comate")
        rendered = run_brief.render(brief)
        self.assertIn("待生成", rendered)
        self.assertNotIn("input_hash", rendered)
        self.assertNotIn("event_id", rendered)

    def test_approval_wait_has_human_status_and_approval_action(self):
        approvals = [{"action": "G2", "approval_id": "approval-123456", "input_hash": "a" * 64,
                      "deadline_at": "2026-09-25T00:00:00+00:00", "created_at": "2026-09-24T00:00:00+00:00"}]
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "required_human_gate": "G2"}, approvals=approvals)
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待审批")
        self.assertEqual(brief["next"]["owner"], "你")
        rendered = run_brief.render(brief)
        self.assertIn("如流审批卡", rendered)
        self.assertNotIn("approval-123456", rendered)
        self.assertNotIn("a" * 64, rendered)


    def test_required_gate_without_open_approval_is_not_misreported_as_waiting(self):
        orch = BriefFixture("INTAKE", {"ok": True, "phase": "INTAKE", "required_human_gate": "G0"})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待处理")
        self.assertEqual(brief["next"]["owner"], "Comate")

    def test_approved_candidate_is_ready_for_worker_continue_without_ids_or_hashes(self):
        approved = {"action": "G2", "approval_id": "approval-secret", "input_hash": "b" * 64,
                    "effective_decision": "APPROVE", "created_at": "2026-09-24T00:01:00+00:00",
                    "resolved_at": "2026-09-24T00:02:00+00:00"}
        orch = BriefFixture("SPEC", {"ok": True, "phase": "SPEC", "required_human_gate": "G2"},
                            approvals=[approved])
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "已批准待提交")
        rendered = run_brief.render(brief)
        self.assertIn("continue", rendered)
        self.assertNotIn("approval-secret", rendered)
        self.assertNotIn("b" * 64, rendered)

    def test_external_intent_recovery_is_actionable(self):
        orch = BriefFixture("SUBMIT", {"ok": False, "reason_code": "RECOVERY_REQUIRED"},
                            pending=[{"operation": "icode.submit", "intent_id": "intent-1"}])
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "待恢复")
        self.assertEqual(brief["next"]["owner"], "Comate")
        self.assertIn("icode.submit", brief["next"]["text"])

    def test_terminal_state_is_reported_as_complete(self):
        orch = BriefFixture("RELEASE_SUCCESS", {"ok": True, "state": "RELEASE_SUCCESS"})
        brief = run_brief.build(orch, "run-1")
        self.assertEqual(brief["status_label"], "已完成")
        self.assertEqual(brief["next"]["owner"], "-")
        self.assertIn("已完成", run_brief.render(brief))

    def test_card_id_status_cli_resolves_target_and_json_keeps_full_events(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        orch = Orchestrator(root)
        run_id = "0123456789abcdef0123456789abcdef"
        orch.state.transition(run_id, "INTAKE", {"requirement_id": "BGW-1", "project": "bgw"})
        summary = {"run_id": run_id, "state": "INTAKE", "status_label": "待处理",
                   "waiting_on": [], "in_flight": [], "blocked": None,
                   "next": {"owner": "Comate", "text": "继续"}}
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
