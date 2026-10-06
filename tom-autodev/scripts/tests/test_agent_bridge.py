"""Contract tests for the thin AgentBridge entry point."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from lock_manager import LockManager
from agent_bridge import _consume_local_infoflow_replies, _ensure_approval_resume_handoff


class AgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.orch = Orchestrator(self.root)
        self.locks = LockManager(self.root / "locks.sqlite")
        self.run_id = "0123456789abcdef0123456789abcdef"
        self.card_id = "BGW-12345"
        self.orch.state.transition(self.run_id, "INTAKE", {
            "requirement_id": self.card_id,
            "project": "bgw",
        })

    def bridge(self, **kwargs):
        from agent_bridge import AgentBridge
        return AgentBridge(self.orch, locks=self.locks, **kwargs)

    def test_drive_forwards_shared_locks_and_injected_run_adapters(self):
        api = object()
        factory = Mock(return_value=api)
        result = {"ok": True, "reason_code": "PARKED", "parked": "PRODUCER_WAIT"}
        with patch("worker_driver.advance", return_value=result) as advance:
            got = self.bridge(ipipe_api_factory=factory, icode_skill="skill-path").drive(self.run_id)
        self.assertEqual(got, result)
        factory.assert_called_once_with(self.run_id)
        advance.assert_called_once()
        self.assertIs(advance.call_args.kwargs["locks"], self.locks)
        self.assertIs(advance.call_args.kwargs["ipipe_api"], api)
        self.assertEqual(advance.call_args.kwargs["icode_skill"], "skill-path")
        json.dumps(got)

    def test_drive_consumes_local_reply_before_worker_advances(self):
        result = {"ok": True, "reason_code": "PARKED", "parked": "PRODUCER_WAIT"}
        with patch(
            "agent_bridge._consume_local_infoflow_replies",
            return_value=[{"approval_id": "approval-1", "effective_decision": "APPROVE"}],
        ) as consume, patch(
            "agent_bridge._ensure_approval_resume_handoff"
        ) as handoff, patch("worker_driver.advance", return_value=result):
            got = self.bridge().drive(self.run_id)
        self.assertEqual(got, result)
        consume.assert_called_once_with(self.orch, self.run_id)
        handoff.assert_called_once_with(self.orch, self.run_id, {"approval-1"})

    def test_a_missing_local_reply_does_not_create_an_approval_or_handoff(self):
        result = {"ok": True, "reason_code": "PARKED", "parked": "APPROVAL_WAIT"}
        with patch(
            "agent_bridge._consume_local_infoflow_replies", return_value=[]
        ) as consume, patch(
            "agent_bridge._ensure_approval_resume_handoff"
        ) as handoff, patch("worker_driver.advance", return_value=result):
            got = self.bridge().drive(self.run_id)
        self.assertEqual(got, result)
        consume.assert_called_once_with(self.orch, self.run_id)
        handoff.assert_called_once_with(self.orch, self.run_id, set())

    def test_local_reply_consumer_settles_only_delivered_infoflow_approvals(self):
        approval = {
            "approval_id": "approval-1",
            "run_id": self.run_id,
            "input_hash": "hash-a",
            "delivery_receipts": [{"channel": "infoflow", "receipt": {"request_id": "r-1"}}],
        }
        self.orch.approvals.pending = Mock(return_value=[approval])
        landed = {
            "approval_id": "approval-1",
            "effective_decision": "APPROVE",
            "reason_code": "OK",
        }
        with patch("orchestrator._infoflow_approval_client", return_value=object()) as factory, \
                patch.object(self.orch, "wait_infoflow_approval", return_value=landed) as wait:
            self.assertEqual(_consume_local_infoflow_replies(self.orch, self.run_id), [landed])
        factory.assert_called_once_with(self.orch)
        wait.assert_called_once()

    def test_local_reply_consumer_does_not_consume_comate_only_or_other_run(self):
        approvals = [
            {
                "approval_id": "comate-only",
                "run_id": self.run_id,
                "input_hash": "hash-a",
                "delivery_receipts": [{"channel": "comate"}],
            },
            {
                "approval_id": "other-run",
                "run_id": "other",
                "input_hash": "hash-b",
                "delivery_receipts": [{"channel": "infoflow"}],
            },
        ]
        self.orch.approvals.pending = Mock(return_value=approvals)
        with patch("orchestrator._infoflow_approval_client", return_value=object()), \
                patch.object(self.orch, "wait_infoflow_approval") as wait:
            self.assertEqual(_consume_local_infoflow_replies(self.orch, self.run_id), [])
        wait.assert_not_called()

    def test_only_fresh_current_gate_approvals_get_a_resume_handoff(self):
        events = [{"state": "PLAN", "event_id": "e-plan", "created_at": "2026-10-06T10:00:00+00:00"}]
        approvals = [
            {"approval_id": "old-g4-a", "action": "G4", "effective_decision": "APPROVE",
             "resolved_at": "2026-10-05T09:00:00+00:00", "input_hash": "h1"},
            {"approval_id": "old-g4-b", "action": "G4", "effective_decision": "APPROVE",
             "resolved_at": "2026-10-05T11:00:00+00:00", "input_hash": "h2"},
            {"approval_id": "new-g4", "action": "G4", "effective_decision": "APPROVE",
             "resolved_at": "2026-10-06T10:05:00+00:00", "input_hash": "h3"},
        ]
        recorded = []
        orch = Mock()
        orch.state.events.return_value = events
        orch.state.incomplete_handoffs.return_value = []
        orch.state.record_handoff.side_effect = lambda run_id, handoff_id, payload: recorded.append(payload)
        orch.approvals.for_run.return_value = approvals
        _ensure_approval_resume_handoff(orch, self.run_id, set())
        self.assertEqual([item["approval_id"] for item in recorded], ["new-g4"])
        recorded.clear()
        _ensure_approval_resume_handoff(orch, self.run_id, {"old-g4-a"})
        self.assertEqual([item["approval_id"] for item in recorded], ["old-g4-a", "new-g4"])

    def test_worker_parked_reasons_are_returned_without_reclassification(self):
        for parked in ("APPROVAL_WAIT", "PRODUCER_WAIT", "IPIPE_MONITOR"):
            with self.subTest(parked=parked):
                result = {"ok": True, "reason_code": "PARKED", "parked": parked, "run_id": self.run_id}
                with patch("worker_driver.advance", return_value=result):
                    got = self.bridge().drive(self.run_id)
                self.assertEqual(got, result)

    def test_submit_draft_preserves_producer_job_and_draft_payload(self):
        job_id = "producer:action-1"
        draft = {"spec": {"acceptance": ["AC-1"]}, "extra": [1, 2]}
        result = {"ok": False, "reason_code": "APPROVAL_REQUIRED", "gate": "G2", "approval_input_hash": "a" * 64}
        with patch("worker_driver.submit_draft", return_value=result) as submit:
            got = self.bridge().submit_draft(self.run_id, job_id, draft)
        self.assertEqual(got, result)
        submit.assert_called_once_with(self.orch, self.run_id, job_id, draft, locks=self.locks)
        self.assertIs(submit.call_args.args[3], draft)
        json.dumps(got)

    def test_continue_uses_resume_only_for_an_approval_handoff(self):
        self.orch.state.record_handoff(self.run_id, "handoff-1", {
            "kind": "APPROVAL_RESUME", "approval_id": "approval-1", "input_hash": "b" * 64,
        })
        result = {"ok": True, "reason_code": "PARKED", "parked": "PRODUCER_WAIT"}
        with patch("worker_driver.resume", return_value=result) as resume, \
                patch("worker_driver.advance") as advance:
            got = self.bridge().continue_run(self.run_id)
        self.assertEqual(got, result)
        resume.assert_called_once()
        advance.assert_not_called()

    def test_continue_drives_from_checkpoint_when_no_approval_handoff_exists(self):
        result = {"ok": True, "reason_code": "PARKED", "parked": "CONTROLLER_STEP"}
        with patch("worker_driver.advance", return_value=result) as advance, \
                patch("worker_driver.resume") as resume:
            got = self.bridge().continue_run(self.run_id)
        self.assertEqual(got, result)
        advance.assert_called_once()
        resume.assert_not_called()
        self.assertIs(advance.call_args.kwargs["locks"], self.locks)

    def test_status_resolves_card_target_and_includes_compact_run_brief(self):
        expected = {"run_id": self.run_id, "state": "INTAKE", "waiting_on": []}
        with patch("run_brief.build", return_value=expected) as build:
            got = self.bridge().status(self.card_id)
        self.assertEqual(got, expected)
        build.assert_called_once_with(self.orch, self.run_id)

    def test_context_exposes_only_current_job_including_saved_draft_state(self):
        action = {
            "action_id": "current", "child_skill": "tom-spec", "phase": "SPEC",
            "result_schema": "spec", "input_hash": "a" * 64,
            "input_artifacts": [{"artifact_id": "upstream"}],
        }
        payload = {"phase": "SPEC", "result_schema": "spec", "mode": "merged"}
        self.orch.state.record_producer_job(self.run_id, "producer:old", payload)
        self.orch.state.record_producer_job(self.run_id, "producer:current", payload)
        before = self.orch.state.events(self.run_id)
        for saved in (False, True):
            with self.subTest(saved=saved):
                if saved:
                    self.orch.state.fulfill_producer_job("producer:current", {"private_draft": "hidden"})
                with patch("worker_driver.classify_next", return_value={"kind": "PRODUCER_WAIT", "action": action}), \
                        patch("worker_driver.advance") as advance:
                    got = self.bridge().context(self.card_id)
                self.assertEqual(got["action"], action)
                self.assertEqual(len(got["producer_jobs"]), 1)
                self.assertEqual(got["producer_jobs"][0]["job_id"], "producer:current")
                self.assertEqual(got["producer_jobs"][0]["draft_present"], saved)
                self.assertEqual(got["producer_jobs"][0]["mode"], "merged")
                self.assertNotIn("private_draft", json.dumps(got))
                self.assertEqual(self.orch.state.events(self.run_id), before)
                advance.assert_not_called()

    def test_context_does_not_create_a_job_and_preserves_block_reason(self):
        with patch("worker_driver.classify_next", return_value={"kind": "BLOCKED", "reason_code": "RECOVERY_REQUIRED"}):
            got = self.bridge().context(self.card_id)
        self.assertEqual(got["decision_kind"], "BLOCKED")
        self.assertEqual(got["decision_reason_code"], "RECOVERY_REQUIRED")
        self.assertEqual(got["producer_jobs"], [])
        self.assertEqual(self.orch.state.pending_producer_jobs(self.run_id), [])

    def test_context_submit_propagates_read_only_to_review_freshness_check(self):
        self.orch.state.transition(self.run_id, "SUBMIT", {})
        protocol = Mock()
        protocol.next.return_value = {
            "ok": True, "state": "SUBMIT", "phase": "SUBMIT", "controller": "submit",
        }
        with patch.object(self.orch, "phase_protocol", return_value=protocol):
            got = self.bridge().context(self.run_id)
        self.assertEqual(got["state"], "SUBMIT")
        self.assertEqual(protocol.next.call_args_list, [
            call(self.run_id, read_only=True), call(self.run_id, read_only=True),
        ])

    def test_stop_resolves_card_target_before_delegating(self):
        expected = {"run_id": self.run_id, "state": "STOPPED"}
        with patch.object(self.orch, "stop", return_value=expected) as stop:
            got = self.bridge().stop(self.card_id)
        self.assertEqual(got, expected)
        stop.assert_called_once_with(self.run_id)

    def test_card_target_without_matching_run_returns_not_found(self):
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, "BGW-99999")
        self.assertEqual(result["reason_code"], "RUN_NOT_FOUND")
        self.assertFalse(result["ok"])

    def test_card_target_with_one_matching_run_resolves_it(self):
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, self.card_id)
        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["state"], "INTAKE")

    def test_card_target_with_multiple_matching_runs_returns_candidates(self):
        second = "fedcba9876543210fedcba9876543210"
        self.orch.state.transition(second, "INTAKE", {
            "requirement_id": self.card_id,
            "project": "bgw",
        })
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, self.card_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "AMBIGUOUS_RUN")
        self.assertEqual({item["run_id"] for item in result["candidates"]}, {self.run_id, second})
        self.assertTrue(all("state" in item for item in result["candidates"]))
        json.dumps(result)


    def test_card_target_ignores_terminal_history_and_resolves_the_single_active_run(self):
        historical = "11111111111111111111111111111111"
        active = "22222222222222222222222222222222"
        card_id = "BGW-ACTIVE"
        self.orch.state.transition(historical, "INTAKE", {"requirement_id": card_id, "project": "bgw"})
        self.orch.state.transition(historical, "STOPPED", {"previous_state": "INTAKE"})
        self.orch.state.transition(active, "INTAKE", {"requirement_id": card_id, "project": "bgw"})
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, card_id)
        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["run_id"], active)

    def test_card_target_with_only_terminal_history_returns_not_found(self):
        self.orch.state.transition(self.run_id, "STOPPED", {"previous_state": "INTAKE"})
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, self.card_id)
        self.assertEqual(result["reason_code"], "RUN_NOT_FOUND")

    def test_run_id_target_is_resolved_directly(self):
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, self.run_id)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["reason_code"], "OK")


if __name__ == "__main__":
    unittest.main()
