"""Contract tests for the thin AgentBridge entry point."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator
from lock_manager import LockManager


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
        submit.assert_called_once_with(self.orch, self.run_id, job_id, draft)
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

    def test_run_id_target_is_resolved_directly(self):
        from agent_bridge import resolve_run_target
        result = resolve_run_target(self.orch, self.run_id)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["reason_code"], "OK")


if __name__ == "__main__":
    unittest.main()
