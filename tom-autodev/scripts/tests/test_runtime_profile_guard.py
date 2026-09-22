"""A cached iPipe runtime must not outlive the run's current profile pin."""
import copy
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profile_repin
from approval_ledger import ApprovalLedger
from clients.ipipe_runtime import IpipeRuntime
from orchestrator import Orchestrator
from state_store import StateStore
from test_ipipe_runtime import PROFILE, REVISIONS, FakeApi, approved, build, canonical_hash, pinned_runtime
from test_orchestrator import _write_profile


class RuntimeProfileGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        profile = copy.deepcopy(PROFILE)
        profile["project_id"] = "bgw"
        _write_profile(self.root, profile)
        self.path = self.root / "config/projects/bgw.yaml"
        self.profile = yaml.safe_load(self.path.read_text())
        self.orch = Orchestrator(self.root)
        self.run = "run-1"
        self.orch.state.transition(self.run, "INTAKE", {
            "project": "bgw", "requirement_id": "BGW-1", "profile_path": str(self.path),
            "profile_hash": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        })
        self.api = FakeApi()
        self.runtime = self._factory()
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    def _factory(self):
        runtime = self.orch.ipipe_runtime(
            self.run, self.api, max_polls=1, sleeper=lambda _: None,
            log_reader=lambda _: {"ok": False},
        )
        self.assertIsInstance(runtime, IpipeRuntime, runtime)
        return runtime

    def _snapshot(self):
        with closing(sqlite3.connect(self.orch.state.database_path)) as connection:
            return "\n".join(connection.iterdump())

    def _blocked_without_effects(self, invoke, reason="PROFILE_CONFLICT"):
        before, calls = self._snapshot(), list(self.api.calls)
        result = invoke()
        self.assertEqual(result["reason_code"], reason, result)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.api.calls, calls)
        return result

    def _drift(self):
        changed = copy.deepcopy(self.profile)
        changed["environment_profile"]["image_digest"] = "sha256:changed"
        self.path.write_text(yaml.safe_dump(changed))

    def _trigger_approval(self, runtime=None, profile=None):
        identity = (runtime or self.runtime).trigger_input_hash(profile or self.profile, REVISIONS)
        self.assertEqual(identity["reason_code"], "OK", identity)
        return approved(self.orch.approvals, "G7", identity["input_hash"], self.run)

    def _bind(self, status="FAIL"):
        item = build(status=status, stages=[{
            "id": "stage-1", "stageName": "unit", "status": status,
        }])
        self.api.candidates = [item]
        self.api.builds["build-1"] = item
        self.assertEqual(self.runtime.discover(self.profile, REVISIONS)["reason_code"], "OK")

    def _rerun_approval(self):
        self._bind()
        observed = self.runtime.monitor("build-1", self.deadline)
        self.assertEqual(observed["reason_code"], "PIPELINE_FAILED", observed)
        identity = self.runtime.rerun_input_hash("stage-1")
        return approved(self.orch.approvals, "G8", identity["input_hash"], self.run)

    def _release_target(self):
        self._bind("SUCCESS")
        target = {
            "module": "baidu/team/app", "pipeline_id": "pipe-1", "target_branch": "main",
            "release_rule": "manual-approval", "stage_classes": ["compile", "unit", "release"],
            "environment_fingerprint": canonical_hash(self.profile["environment_profile"]),
            "expected_revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "test-rev"},
            "expected_branches": {"baidu/team/app": "main", "baidu/team/app-tests": "main"},
            "source_revisions": {"business": "app-rev", "tests": "test-rev"},
            "binding_hash": "a" * 64,
        }
        plan = {
            "version": 1, "run_id": self.run, "modules": {"baidu/team/app": target},
            "required_modules": ["baidu/team/app"], "revision_set": REVISIONS,
            "profile_content_hash": canonical_hash(self.profile),
        }
        self.orch.state.transition(self.run, "IPIPE", {
            "pipeline_plan": {**plan, "plan_hash": canonical_hash(plan)},
        })
        self.api.releases = [{
            "id": "release-1", "module": "baidu/team/app", "branch": "main",
            "pipelineBuildId": "build-1", "revisions": target["expected_revisions"],
            "releaseRule": "manual-approval", "status": "SUCCESS",
        }]
        return target

    def test_cached_factory_runtime_rejects_drift_before_trigger_or_intent(self):
        approval = self._trigger_approval()
        self.api.builds["build-1"] = build()
        self._drift()
        self.assertEqual(self.orch._runtime_profile(self.run)["reason_code"], "PROFILE_CONFLICT")
        self._blocked_without_effects(lambda: self.runtime.trigger(self.profile, REVISIONS, approval))

    def test_discovery_cannot_persist_ownership_after_drift(self):
        self.api.candidates = [build()]
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.discover(self.profile, REVISIONS))

    def test_rerun_cannot_write_or_claim_an_intent_after_drift(self):
        approval = self._rerun_approval()
        self.api.builds["build-1"] = build(stages=[{"id": "stage-1", "status": "RUNNING"}])
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.rerun("stage-1", approval))

    def test_monitor_cannot_persist_stage_or_failure_evidence_after_drift(self):
        self._bind()
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.monitor("build-1", self.deadline))

    def test_planned_release_cannot_persist_a_verifier_result_after_drift(self):
        target = self._release_target()
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.verify_planned_release("build-1", target))

    def test_missing_profile_file_blocks_a_cached_runtime(self):
        approval = self._trigger_approval()
        self.path.unlink()
        self._blocked_without_effects(
            lambda: self.runtime.trigger(self.profile, REVISIONS, approval), "PROJECT_NOT_READY",
        )

    def test_mutating_the_constructor_argument_cannot_mutate_the_runtime_binding(self):
        supplied = copy.deepcopy(self.profile)
        runtime = IpipeRuntime(
            self.orch.state, self.orch.approvals, self.run, self.api,
            validated_profile=supplied, profile_hash=self.runtime.profile_hash,
        )
        supplied["environment_profile"]["image_digest"] = "sha256:changed"
        self.api.candidates = [build()]
        self._blocked_without_effects(lambda: runtime.discover(supplied, REVISIONS))
        # The original valid profile must still work; rejecting both would hide aliasing.
        self.assertEqual(runtime.discover(self.profile, REVISIONS)["reason_code"], "OK")

    def test_mutating_the_public_cached_dict_cannot_redefine_the_pin(self):
        self.runtime.validated_profile["environment_profile"]["image_digest"] = "sha256:changed"
        self.api.candidates = [build()]
        self._blocked_without_effects(lambda: self.runtime.discover(self.runtime.validated_profile, REVISIONS))

    def test_latest_approved_repin_rejects_old_objects_and_accepts_a_new_factory_runtime(self):
        original_approval = self._trigger_approval()
        runtimes = [(self.runtime, self.profile, original_approval)]
        for index, change in enumerate((
            lambda p: p.update(submission_policy="one_cr_per_repo"),
            lambda p: p["pipeline_profile"].update(stage_classes=["compile", "unit", "release", "integration"]),
        )):
            previous = self.root / f"previous-{index}.yaml"
            previous.write_bytes(self.path.read_bytes())
            current = yaml.safe_load(self.path.read_text())
            change(current)
            self.path.write_text(yaml.safe_dump(current))
            prepared = profile_repin.plan(self.orch, self.run, previous)
            self.assertEqual(prepared["reason_code"], "OK", prepared)
            approval = approved(self.orch.approvals, profile_repin.ACTION, prepared["input_hash"], self.run)
            applied = profile_repin.apply(self.orch, self.run, approval["approval_id"], previous)
            self.assertEqual(applied["reason_code"], "OK", applied)
            for stale, profile, trigger_approval in runtimes:
                self._blocked_without_effects(lambda: stale.trigger(profile, REVISIONS, trigger_approval))
            fresh = self._factory()
            runtimes.append((fresh, current, self._trigger_approval(fresh, current)))
        self.api.builds["build-1"] = build()
        fresh, current, trigger_approval = runtimes[-1]
        result = fresh.trigger(current, REVISIONS, trigger_approval)
        self.assertEqual(result["reason_code"], "OK", result)
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in self.api.calls), 1)

    def test_completed_trigger_receipt_remains_readable_without_writes_after_drift(self):
        approval = self._trigger_approval()
        self.api.builds["build-1"] = build()
        completed = self.runtime.trigger(self.profile, REVISIONS, approval)
        self.assertEqual(completed["reason_code"], "OK", completed)
        self._drift()
        before, calls = self._snapshot(), list(self.api.calls)
        self.assertEqual(self.runtime.trigger(self.profile, REVISIONS, approval), completed)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.api.calls, calls)

    def test_completed_rerun_receipt_remains_readable_without_writes_after_drift(self):
        approval = self._rerun_approval()
        self.api.builds["build-1"] = build(stages=[{"id": "stage-1", "status": "RUNNING"}])
        completed = self.runtime.rerun("stage-1", approval)
        self.assertEqual(completed["reason_code"], "OK", completed)
        self._drift()
        before, calls = self._snapshot(), list(self.api.calls)
        self.assertEqual(self.runtime.rerun("stage-1", approval), completed)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.api.calls, calls)

    def test_unknown_trigger_cannot_be_reconciled_into_a_new_receipt_after_drift(self):
        approval = self._trigger_approval()
        self.api.trigger_result = TimeoutError("unknown")
        pending = self.runtime.trigger(self.profile, REVISIONS, approval)
        self.assertEqual(pending["reason_code"], "TRIGGER_RESULT_UNKNOWN", pending)
        self.api.candidates = [build()]
        self.api.builds["build-1"] = build()
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.trigger(self.profile, REVISIONS, approval))

    def test_unknown_rerun_cannot_be_reconciled_into_a_new_receipt_after_drift(self):
        approval = self._rerun_approval()
        self.api.rerun_result = TimeoutError("unknown")
        pending = self.runtime.rerun("stage-1", approval)
        self.assertEqual(pending["reason_code"], "RERUN_RESULT_UNKNOWN", pending)
        self.api.builds["build-1"] = build(stages=[{"id": "stage-1", "status": "RUNNING"}])
        self._drift()
        self._blocked_without_effects(lambda: self.runtime.rerun("stage-1", approval))

    def _drift_during_query(self, method):
        original = getattr(self.api, method)
        checkpoint = {}

        def query(*args, **kwargs):
            result = original(*args, **kwargs)
            self._drift()
            checkpoint["state"] = self._snapshot()
            return result

        setattr(self.api, method, query)
        return checkpoint

    def test_drift_during_discovery_does_not_save_build_ownership(self):
        self.api.candidates = [build()]
        checkpoint = self._drift_during_query("builds_by_revision")
        result = self.runtime.discover(self.profile, REVISIONS)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])

    def test_drift_during_trigger_discovery_does_not_reach_the_platform_write(self):
        approval = self._trigger_approval()
        self.api.builds["build-1"] = build()
        checkpoint = self._drift_during_query("builds_by_revision")
        result = self.runtime.trigger(self.profile, REVISIONS, approval)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])
        self.assertFalse(any(call[0] == "trigger_by_revision" for call in self.api.calls))

    def test_drift_during_trigger_confirmation_does_not_save_a_receipt(self):
        approval = self._trigger_approval()
        self.api.builds["build-1"] = build()
        checkpoint = self._drift_during_query("build_by_id")
        result = self.runtime.trigger(self.profile, REVISIONS, approval)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in self.api.calls), 1)

    def test_drift_during_rerun_confirmation_does_not_save_a_receipt(self):
        approval = self._rerun_approval()
        self.api.builds["build-1"] = build(stages=[{"id": "stage-1", "status": "RUNNING"}])
        checkpoint = self._drift_during_query("build_by_id")
        result = self.runtime.rerun("stage-1", approval)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_drift_during_rerun_log_read_does_not_claim_or_write(self):
        self._bind("SUCCESS")
        self.api.builds["build-1"]["stageBuilds"][0]["jobBuildBeans"] = [{
            "id": "job-1", "jobName": "tests", "status": "SUCC",
            "logs": [{"url": "https://example.test/job-1"}],
        }]
        log = {"ok": True, "success_ratios": [0.0], "failed_cases": ["case-1"]}
        self.runtime.log_reader = lambda _: log
        observed = self.runtime.monitor("build-1", self.deadline)
        self.assertEqual(observed["reason_code"], "JOB_SUCCEEDED_WITH_FAILED_CASES", observed)
        identity = self.runtime.rerun_input_hash("stage-1")
        approval = approved(self.orch.approvals, "G8", identity["input_hash"], self.run)

        def changed_log(_):
            self._drift()
            return log

        self.runtime.log_reader = changed_log
        before = self._snapshot()
        result = self.runtime.rerun("stage-1", approval)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(any(call[0] == "manual_execute_stage" for call in self.api.calls))

    def test_drift_during_stage_query_does_not_save_stage_ownership(self):
        self._bind()
        checkpoint = self._drift_during_query("pipeline_stage_info")
        result = self.runtime.monitor("build-1", self.deadline)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])

    def test_drift_during_failure_query_does_not_save_failure_evidence(self):
        self._bind()
        checkpoint = self._drift_during_query("failed_jobs")
        result = self.runtime.monitor("build-1", self.deadline)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])

    def test_monitor_rechecks_the_profile_after_waiting_for_another_poll(self):
        self._bind("RUNNING")
        self.runtime.max_polls = 2
        checkpoint = {}

        def change_between_polls(_):
            self._drift()
            self.api.stages["build-1"] = [{"id": "stage-2", "status": "FAIL"}]
            checkpoint["state"] = self._snapshot()
            checkpoint["calls"] = list(self.api.calls)

        self.runtime.sleeper = change_between_polls
        result = self.runtime.monitor("build-1", self.deadline)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])
        self.assertEqual(self.api.calls, checkpoint["calls"])

    def test_drift_during_release_query_does_not_save_a_verifier_result(self):
        target = self._release_target()
        checkpoint = self._drift_during_query("release_info")
        result = self.runtime.verify_planned_release("build-1", target)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT", result)
        self.assertEqual(self._snapshot(), checkpoint["state"])

    def test_release_queries_remain_read_only_after_drift(self):
        target = self._release_target()
        verified = self.runtime.verify_planned_release("build-1", target)
        self.assertEqual(verified["reason_code"], "OK", verified)
        self._drift()
        before = self._snapshot()
        self.assertEqual(self.runtime.verify_release("build-1", REVISIONS)["reason_code"], "OK")
        self.assertEqual(self.runtime.verify_release_of_build("build-1")["reason_code"], "OK")
        self.assertEqual(self._snapshot(), before)

    def test_legacy_fixture_without_profile_path_retains_its_bound_runtime(self):
        state = StateStore(self.root / "legacy-state.sqlite")
        ledger = ApprovalLedger(self.root / "legacy-approvals.sqlite")
        state.transition("run-1", "INTAKE", {"profile_hash": canonical_hash(PROFILE)})
        runtime = pinned_runtime(state, ledger, self.api)
        self.api.builds["build-1"] = build()
        approval = approved(ledger, "G7", runtime.trigger_input_hash(PROFILE, REVISIONS)["input_hash"])
        result = runtime.trigger(PROFILE, REVISIONS, approval)
        self.assertEqual(result["reason_code"], "OK", result)


if __name__ == "__main__":
    unittest.main()
