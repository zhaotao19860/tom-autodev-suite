"""Regression for a copied project topic overriding the current iCafe card."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_bridge import AgentBridge
from collaboration import build_collaboration_binding, intake_input_hash, intake_prerequisites_valid
from orchestrator import Orchestrator
from test_collaboration import FakeGroupClient
from test_orchestrator import PROFILE, _approve_for_run, _requirement_snapshot, _start, _write_profile


class CollaborationIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _write_profile(self.root, PROFILE)
        self.orch = Orchestrator(self.root)
        self.source_run = _start(self.orch)["run_id"]
        self.payload = self.orch.state.events(self.source_run)[0]["payload"]

    def stale_run(self, *, version="v2", mutation="topic"):
        payload = copy.deepcopy(self.payload)
        run_id = f"stale-{version}-{mutation}"
        binding = payload["collaboration_binding"]
        binding["run_id"] = run_id
        binding["session_idempotency_key"] = f"infoflow.group.create:{run_id}"
        if mutation == "topic":
            binding["group_topic"] = "支持基于url负载均衡"
            binding["group_name"] = "BGW-1-支持基于url负载均衡"
        elif mutation == "title":
            binding["card_title"] = "Previous requirement"
        else:
            binding["card_content_hash"] = "f" * 64
        if version == "v1":
            payload.pop("intake_hash_version")
        payload["g0_input_hash"] = intake_input_hash(payload)
        self.orch.state.transition(run_id, "INTAKE", payload)
        return run_id, payload

    def test_snapshot_title_wins_even_when_caller_passes_old_topic(self):
        binding = build_collaboration_binding(
            "run", "bgw", "BGW-1995", "p", "【BGW】syncookie问题修复；", "c",
            PROFILE["approval_channels"]["role_members"], "支持基于url负载均衡",
        )["binding"]
        self.assertEqual(binding["group_topic"], "syncookie问题修")
        self.assertEqual(binding["group_name"], "BGW-1995-syncookie问题修")

    def test_rehashed_stale_binding_is_invalid_in_both_hash_versions(self):
        for version in ("v1", "v2"):
            for mutation in ("topic", "title", "content_hash"):
                with self.subTest(version=version, mutation=mutation):
                    run_id, payload = self.stale_run(version=version, mutation=mutation)
                    self.assertFalse(intake_prerequisites_valid(payload, run_id))

    def test_bridge_blocks_before_workers_or_adapters_are_called(self):
        run_id, _ = self.stale_run()
        factory = Mock()
        bridge = AgentBridge(self.orch, locks=self.orch.recovery.locks,
                             ipipe_api_factory=factory)
        self.orch.state.record_handoff(run_id, "old-approved", {"kind": "APPROVAL_RESUME"})
        with patch("worker_driver.advance") as advance, patch("worker_driver.resume") as resume:
            for operation in (bridge.drive, bridge.continue_run):
                self.assertEqual(operation(run_id)["reason_code"], "COLLABORATION_BINDING_MISMATCH")
        advance.assert_not_called()
        resume.assert_not_called()
        factory.assert_not_called()

    def test_direct_create_cannot_reuse_stale_pending_or_completed_intent(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                run_id, payload = self.stale_run(version="v1" if completed else "v2")
                approval = _approve_for_run(self.orch, run_id, "G0", payload["g0_input_hash"])
                binding = payload["collaboration_binding"]
                request = {key: binding[key] for key in (
                    "run_id", "project", "card_id", "profile_hash", "group_name", "owner",
                    "member_snapshot", "roles", "friendlyLevel", "card_content_hash",
                )}
                request.update(input_hash=payload["g0_input_hash"], approval_id=approval["approval_id"])
                key = binding["session_idempotency_key"]
                claim = self.orch.state.claim_intent(run_id, "infoflow.group.create", key, request)
                if completed:
                    self.orch.state.receipt(claim["intent"]["intent_id"], {
                        "run_id": run_id, "group_id": "123", "group_name": binding["group_name"],
                    }, [])
                before = self.orch.state.result_by_idempotency_key(key)
                pending = self.orch.state.pending_intents(run_id)
                client = FakeGroupClient()
                result = self.orch.collaboration_session(client).create(
                    run_id, "bgw", {"id": "BGW-1"}, PROFILE["approval_channels"]["role_members"],
                    approval_id=approval["approval_id"], input_hash=payload["g0_input_hash"],
                )
                self.assertEqual(result["reason_code"], "G0_BINDING_MISMATCH")
                self.assertEqual(client.create_calls, [])
                self.assertEqual(client.reconcile_calls, [])
                self.assertEqual(self.orch.state.result_by_idempotency_key(key), before)
                self.assertEqual(self.orch.state.pending_intents(run_id), pending)

    def test_stop_and_fresh_preserve_old_run_but_do_not_reuse_g0(self):
        run_id, payload = self.stale_run()
        old_approval = _approve_for_run(self.orch, run_id, "G0", payload["g0_input_hash"])
        self.orch.stop(run_id)
        fresh = self.orch.start("BGW-1", "bgw", requirement_snapshot=_requirement_snapshot(),
                                fresh_token="explicit-restart")
        fresh_payload = self.orch.state.events(fresh["run_id"])[0]["payload"]
        self.assertEqual(self.orch.state.events(run_id)[-1]["state"], "STOPPED")
        self.assertEqual(self.orch.state.events(run_id)[0]["payload"], payload)
        self.assertNotEqual(fresh_payload["g0_input_hash"], payload["g0_input_hash"])
        self.assertTrue(intake_prerequisites_valid(fresh_payload, fresh["run_id"]))
        client = FakeGroupClient()
        rejected = self.orch.collaboration_session(client).create(
            fresh["run_id"], "bgw", {"id": "BGW-1"}, PROFILE["approval_channels"]["role_members"],
            approval_id=old_approval["approval_id"], input_hash=fresh_payload["g0_input_hash"],
        )
        self.assertEqual(rejected["reason_code"], "G0_BINDING_MISMATCH")
        self.assertEqual(client.create_calls, [])


if __name__ == "__main__":
    unittest.main()
