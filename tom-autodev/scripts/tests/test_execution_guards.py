"""A pinned run cannot add effects through an execution entry after policy drift."""

import copy
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchestrator as entry
import approval_watch
import profile_repin
import submit_descriptor
import worker_driver
import workflow_spec
from clients.icafe_client import CafeClient
from clients.icode_ai_review import IcodeAiReview
from clients.icode_client import IcodeClient
from clients.icode_runtime import IcodeRuntime
from clients.ipipe_client import IpipeClient
from clients.ku_client import KuClient
from collaboration import CollaborationSession
from approval_delivery import InfoflowApprovalTransport
from knowledge_sync import KnowledgeSync
from ipipe_watch import IpipeWatcher
from test_ipipe_runtime import FakeApi, PROFILE, REVISIONS, approved, build, pinned_runtime


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.owner = entry.Orchestrator(self.root)
        self.run_id = "run-1"
        self.pin = workflow_spec.canonical_hash()
        self.owner.state.transition(self.run_id, "SUBMIT", {"workflow_spec_hash": self.pin})
        self.calls = []

    def snapshot(self):
        values = []
        for path in sorted(self.root.rglob("*.sqlite")):
            with sqlite3.connect(path) as connection:
                values.append((str(path), tuple(connection.iterdump())))
        return values

    def effect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"ok": False, "reason_code": "EFFECT_REACHED"}

    def blocked(self, invoke):
        before = self.snapshot()
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            result = invoke()
        self.assertEqual(result.get("reason_code"), "WORKFLOW_SPEC_DRIFT", result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["pinned_spec_hash"], self.pin)
        self.assertEqual(result["current_spec_hash"], "new-policy")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_cli_submit_refuses_before_descriptor_build(self):
        approval = approved(self.owner.approvals, "G7", "approved-submit")
        with patch.object(submit_descriptor, "build_and_archive", side_effect=self.effect):
            self.blocked(lambda: entry._submit(
                self.owner, self.run_id, "task-1", approval["approval_id"], "/unused"))

    def test_cached_review_completion_cannot_archive_another_descriptor(self):
        protocol = SimpleNamespace(complete=lambda *args: {
            "ok": True, "phase_complete": True, "phase": "REVIEW", "task_id": "task-1"})
        with patch.object(self.owner, "phase_protocol", return_value=protocol), patch.object(
            submit_descriptor, "build_and_archive", side_effect=self.effect
        ):
            self.blocked(lambda: self.owner.complete_phase(
                self.run_id, {}, knowledge_sync=object()))

    def test_submit_to_ipipe_refuses_before_runtime_but_allows_pure_receipt_replay(self):
        change = {"change_set_id": "change-1", "revision_set_id": "revisions-1", "input_hash": "hash"}
        runtime = SimpleNamespace(run_id=self.run_id, submit=self.effect)
        self.blocked(lambda: self.owner.submit_to_ipipe(self.run_id, change, {}, icode_runtime=runtime))
        receipt = {"ok": True, "reason_code": "OK", "state": "IPIPE"}
        self.owner.state.save_idempotency_result("submit-to-ipipe:run-1:change-1:revisions-1", receipt)
        before = self.snapshot()
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            self.assertEqual(self.owner.submit_to_ipipe(self.run_id, change, {}, icode_runtime=runtime), receipt)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_orchestrator_mutation_entry_matrix(self):
        owner, run = self.owner, self.run_id
        policy = {"comate": ["a@test.example"], "infoflow": ["a@test.example"]}
        approval_id = owner.approvals.request("G8", "hash", ["comate", "infoflow"], run_id=run, member_policy=policy)["approval_id"]
        cases = {
            "advance": lambda: owner.advance(run, "IPIPE", {}),
            "failure routing": lambda: owner.route_failure(run, "CODE_FAILURE", {}),
            "rebuild recovery": lambda: owner.recover_rebuilt_change_set(run, "task", "plan"),
            "stale plan recovery": lambda: owner.recover_stale_rebuilt_plan(run, "task", "SUBMIT", "plan", {"business": "b", "tests": "t"}),
            "stale submit recovery": lambda: owner.recover_stale_submit(run, "task", "plan"),
            "summary archival": lambda: owner.optimize(run, "build"),
            "approval request": lambda: owner.request_infoflow_approval(run, "G7", "hash", member_policy={"comate": ["a@test.example"], "infoflow": ["a@test.example"]}, comate_client=SimpleNamespace(request=self.effect), infoflow_client=SimpleNamespace(request=self.effect)),
            "approval reply": lambda: owner.receive_infoflow_reply(run, approval_id, "hash", {}),
            "approval wait": lambda: owner.wait_infoflow_approval(run, approval_id, "hash", SimpleNamespace(wait=self.effect), 0),
            "approval heartbeat": lambda: owner.heartbeat_infoflow_approval(run, approval_id, datetime.now(timezone.utc).isoformat()),
            "approval timeout": lambda: owner.timeout_infoflow_approval(run, approval_id, "hash"),
            "approval reissue": lambda: owner.reissue_infoflow_approval(run, "G8", "hash", member_policy=policy, infoflow_client=object()),
            "descriptor": lambda: submit_descriptor.build_and_archive(owner, run, "task"),
            "profile repin": lambda: profile_repin.apply(owner, run, "approval", self.root / "old.yaml"),
            "profile approval": lambda: profile_repin.request(owner, run, self.root / "old.yaml", comate_client=object(), infoflow_client=object()),
            "worker controller": lambda: worker_driver.execute_controller(owner, run),
            "worker auto": lambda: worker_driver.execute_auto(owner, run),
            "worker advance": lambda: worker_driver.advance(owner, run),
            "worker resume": lambda: worker_driver.resume(owner, run),
        }
        for name, invoke in cases.items():
            with self.subTest(entry=name):
                self.calls.clear()
                self.blocked(invoke)

    def test_direct_pipeline_runtime_and_compatibility_entries(self):
        api = FakeApi()
        api.candidates = [build()]
        api.builds = {"build-1": build()}
        runtime = pinned_runtime(self.owner.state, self.owner.approvals, api, max_polls=1)
        approval = approved(self.owner.approvals, "G7", runtime.trigger_input_hash(PROFILE, REVISIONS)["input_hash"])
        discovered = runtime.discover(PROFILE, REVISIONS)
        self.assertTrue(discovered["ok"], discovered)
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        client = IpipeClient(runtime=runtime)
        cases = {
            "trigger": lambda: runtime.trigger(PROFILE, REVISIONS, approval),
            "rerun": lambda: runtime.rerun("stage-1", approval),
            "adopt/discover": lambda: runtime.discover(PROFILE, REVISIONS),
            "monitor writes ownership": lambda: runtime.monitor(discovered["build_id"], deadline),
            "compat trigger": lambda: client.trigger(PROFILE, REVISIONS, approval),
            "compat rerun": lambda: client.rerun("stage-1", approval),
            "compat discover": lambda: client.discover(PROFILE, REVISIONS),
            "compat monitor": lambda: client.monitor(discovered["build_id"], deadline),
        }
        for name, invoke in cases.items():
            with self.subTest(entry=name):
                prior = copy.deepcopy(api.calls)
                self.blocked(invoke)
                self.assertEqual(api.calls, prior)

    def test_direct_publication_and_collaboration_entries(self):
        ku = KuClient(state_store=self.owner.state, run_id=self.run_id, repo_id="repo", username="user", transport=SimpleNamespace(run=self.effect))
        cafe = CafeClient(state_store=self.owner.state, run_id=self.run_id, transport=SimpleNamespace(run=self.effect), preflight=False)
        sync = KnowledgeSync(state_store=self.owner.state, ku_client=ku, cafe_client=cafe, parent_doc_id="parent", card_id="BGW-1", run_id=self.run_id)
        collaboration = CollaborationSession(self.owner.state, SimpleNamespace(create_or_reuse=self.effect, send_markdown=self.effect), approvals=self.owner.approvals)
        cases = {
            "KU root": lambda: ku.ensure_run_root("parent", "title", "body"),
            "KU artifact": lambda: ku.create_artifact("parent", "title", "body"),
            "KU index": lambda: ku.update_index("doc", {}),
            "iCafe comment": lambda: cafe.comment("BGW-1", "body", "key"),
            "iCafe status": lambda: cafe.update_status("BGW-1", "IN_PROGRESS", "OPEN"),
            "phase publication": lambda: sync.publish_phase(self.run_id, {"content_hash": "hash"}),
            "group create": lambda: collaboration.create(self.run_id, "bgw", {}, {}),
            "group message": lambda: collaboration.send_message(self.run_id, "message", [], "key"),
            "summary build": lambda: self.owner.run_summary().build(self.run_id),
        }
        for name, invoke in cases.items():
            with self.subTest(entry=name):
                self.calls.clear()
                self.blocked(invoke)

    def test_icode_and_ai_review_cannot_bypass_controller(self):
        runtime = IcodeRuntime(state_store=self.owner.state, approval_ledger=self.owner.approvals,
            artifact_store=self.owner.artifacts, workspace_manager=self.owner.workspaces,
            run_id=self.run_id, worktree_bindings={}, system_skill_path=self.root,
            argv_transport=SimpleNamespace(run=self.effect))
        approval = approved(self.owner.approvals, "G7", "hash")
        change = {"input_hash": "hash", "repo_path": str(self.root)}
        with patch.object(runtime, "_validate_change_set", return_value=None), patch.object(runtime, "preflight", side_effect=self.effect):
            self.blocked(lambda: runtime.submit(change, approval))
            self.blocked(lambda: IcodeClient(runtime=runtime).submit(change, approval))
        review = IcodeAiReview(state_store=self.owner.state, run_id=self.run_id,
            argv_transport=SimpleNamespace(run=self.effect), working_directory=self.root,
            artifact_store=self.owner.artifacts)
        with patch.object(review, "_cli", side_effect=self.effect):
            self.blocked(lambda: review.start("1", "revision"))
            self.calls.clear()
            self.blocked(lambda: review.poll("conversation"))

    def test_drift_still_allows_read_only_recovery_status_and_stop(self):
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            before = self.snapshot()
            self.assertEqual(self.owner.status(self.run_id)["state"], "SUBMIT")
            self.assertFalse(self.owner.resume(self.run_id)["execution_performed"])
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.owner.stop(self.run_id)["state"], "STOPPED")

    def test_guard_errors_are_not_disguised_as_domain_failures(self):
        with patch.object(workflow_spec, "canonical_hash", side_effect=ValueError("invalid policy")):
            with self.assertRaisesRegex(ValueError, "invalid policy"):
                self.owner.submit_to_ipipe(self.run_id, {
                    "change_set_id": "change", "revision_set_id": "revision", "input_hash": "hash"
                }, {}, icode_runtime=SimpleNamespace(run_id=self.run_id, submit=self.effect))

    def test_approval_resolution_and_background_writers_refuse_drift(self):
        approval = approved(self.owner.approvals, "G7", "hash")
        self.blocked(lambda: self.owner.approve(approval["approval_id"], "APPROVE", "hash", "comate", run_id=self.run_id, responder="owner@example.test"))
        self.blocked(lambda: approval_watch.notify_ide_turn(self.owner, self.run_id, SimpleNamespace(send_markdown=self.effect)))

    def test_background_tick_does_not_create_notices_or_handoffs(self):
        watcher = approval_watch.ApprovalWatcher(self.owner, lambda: SimpleNamespace(wait=self.effect), SimpleNamespace(send_markdown=self.effect, ensure_ready=self.effect))
        before = self.snapshot()
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            results = watcher.tick()
        self.assertTrue(any(result.get("reason_code") == "WORKFLOW_SPEC_DRIFT" for result in results), results)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_pipeline_tick_refuses_before_monitor_or_notice(self):
        self.owner.state.transition(self.run_id, "IPIPE", {})
        watcher = IpipeWatcher(self.owner, lambda run: SimpleNamespace(monitor=self.effect), SimpleNamespace(send_markdown=self.effect))
        before = self.snapshot()
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            results = watcher.tick()
        self.assertEqual(results[0].get("reason_code"), "WORKFLOW_SPEC_DRIFT", results)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_optimization_recovery_guard_precedes_archive_or_apply(self):
        summary = self.owner.run_summary()
        self.blocked(lambda: summary.propose({"run_id": self.run_id}, [self.root]))
        self.owner.state.save_optimization_proposal({
            "proposal_id": "proposal", "run_id": self.run_id,
            "candidate_hash": "candidate", "envelope_hash": "envelope"})
        self.blocked(lambda: summary.apply("proposal", "approval"))
        self.blocked(lambda: summary.heartbeat("proposal", "owner"))

    def test_direct_phase_write_entries_and_policy_errors_are_guarded(self):
        protocol = self.owner.phase_protocol(object())
        for name, invoke in {
            "completion": lambda: protocol.complete(self.run_id, {}),
            "pipeline ingestion": lambda: protocol.ingest_ipipe_evidence(self.run_id, {}),
            "release ingestion": lambda: protocol.ingest_release_evidence(self.run_id, {}, {}),
        }.items():
            with self.subTest(entry=name):
                self.blocked(invoke)
                with patch.object(workflow_spec, "canonical_hash", side_effect=ValueError("invalid policy")):
                    with self.assertRaisesRegex(ValueError, "invalid policy"):
                        invoke()

    def test_planned_execution_entries_guard_before_body_and_preserve_errors(self):
        import pipeline_plan

        protocol = self.owner.phase_protocol(object())
        api = FakeApi()
        runtime = pinned_runtime(self.owner.state, self.owner.approvals, api)
        cases = (
            ("action cache", protocol.next, (self.run_id,), protocol, "_next"),
            ("pipeline finalization", protocol.finalize_planned_ipipe,
             (self.run_id,), protocol, "next"),
            ("planned release verification", runtime.verify_planned_release,
             ("build-1", {}), pipeline_plan, "frozen_plan"),
        )
        for name, invoke, arguments, body_owner, first_body_call in cases:
            with self.subTest(entry=name), patch.object(
                body_owner, first_body_call, side_effect=self.effect
            ):
                with patch.object(
                    self.owner.state, "events", wraps=self.owner.state.events
                ) as events:
                    self.blocked(lambda: invoke(*arguments))
                    # Only the guard may read the ledger; no action-cache, plan, build,
                    # or release work may begin before the policy pin is checked.
                    events.assert_called_once_with(self.run_id)
                failures = (
                    (workflow_spec, "canonical_hash", ValueError("invalid policy")),
                    (self.owner.state, "events", sqlite3.DatabaseError("unreadable ledger")),
                )
                for source, method, error in failures:
                    with self.subTest(error=type(error).__name__):
                        before = self.snapshot()
                        with patch.object(source, method, side_effect=error):
                            with self.assertRaises(type(error)) as caught:
                                invoke(*arguments)
                        self.assertIs(caught.exception, error)
                        self.assertEqual(self.calls, [])
                        self.assertEqual(self.snapshot(), before)
                self.assertEqual(api.calls, [])

    def test_unarchived_ai_poll_and_pipeline_queries_remain_read_only(self):
        review = IcodeAiReview(state_store=self.owner.state, run_id=self.run_id,
            argv_transport=object(), working_directory=self.root)
        with patch.object(review, "_cli", return_value={"reason_code": "NO_CLI"}), patch.object(
            workflow_spec, "canonical_hash", return_value="new-policy"
        ):
            self.assertEqual(review.poll("conversation")["reason_code"], "NO_CLI")
        runtime = pinned_runtime(self.owner.state, self.owner.approvals, FakeApi())
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            self.assertTrue(runtime.trigger_input_hash(PROFILE, REVISIONS)["ok"])
            self.assertEqual(runtime.verify_release("unknown", REVISIONS)["reason_code"], "BUILD_OWNERSHIP_UNVERIFIED")

    def test_legacy_run_execution_and_new_run_pin_are_unchanged(self):
        from test_fake_e2e import FakeE2ETests, snapshot
        fixture = FakeE2ETests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with patch.object(workflow_spec, "canonical_hash", return_value="new-policy"):
            started = fixture.orchestrator.start("BGW-100", "bgw", requirement_snapshot=snapshot("BGW-100"))
            events = fixture.orchestrator.state.events(started["run_id"])
            self.assertEqual(events[0]["payload"]["workflow_spec_hash"], "new-policy")
            self.assertTrue(fixture.orchestrator.next(started["run_id"])["ok"])
            self.owner.state.transition("legacy", "SUBMIT", {})
            self.assertNotEqual(submit_descriptor.build_and_archive(self.owner, "legacy", "task")["reason_code"], "WORKFLOW_SPEC_DRIFT")

    def test_direct_approval_transport_cannot_send_or_record_reply_after_drift(self):
        transport = InfoflowApprovalTransport(self.owner.state, SimpleNamespace(send_markdown=self.effect))
        request = {
            "run_id": self.run_id, "approval_id": "approval", "input_hash": "hash", "channel": "infoflow",
            "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "member_policy": {"comate": ["a@test.example"], "infoflow": ["a@test.example"]},
        }
        self.blocked(lambda: transport.request(request))

    def test_stop_hook_leaves_approved_handoff_pending_after_drift(self):
        approval = approved(self.owner.approvals, "G7", "hash")
        self.owner.state.record_handoff(self.run_id, "handoff", {
            "kind": "APPROVAL_RESUME", "event_id": self.owner.state.events(self.run_id)[-1]["event_id"],
            "approval_id": approval["approval_id"], "input_hash": "hash"})
        self.blocked(lambda: approval_watch.auto_resume_from_hook(self.owner, {"hook_event_name": "Stop"}))

    def test_approval_transport_cached_wait_cannot_persist_a_new_reply(self):
        transport = InfoflowApprovalTransport(self.owner.state, SimpleNamespace(send_markdown=lambda *args: {"message_key": "sent"}))
        request = {
            "run_id": self.run_id, "approval_id": "approval", "input_hash": "hash", "channel": "infoflow",
            "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "member_policy": {"comate": ["a@test.example"], "infoflow": ["a@test.example"]},
        }
        receipt = transport.request(request)
        transport.reply_consumer = SimpleNamespace(resolve=lambda record: {
            "reply_id": "reply", "decision": "APPROVE", "responder": "a@test.example",
            "received_at": datetime.now(timezone.utc).isoformat()})
        self.blocked(lambda: transport.wait(receipt["request_id"], 0))


if __name__ == "__main__":
    unittest.main()
