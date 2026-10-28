import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
import threading
import unittest
from typing import Any
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport, IpipeTransportError
from clients.ipipe_runtime import IpipeRuntime, _normalize_stage, _stage_passed, evidence_outcome
from project_registry import validate_profile
from schema_validator import validate_named_schema
from state_store import StateStore


PROFILE = {
    "project_id": "demo",
    "business_repos": [{"path": "/repo/app", "module": "baidu/team/app", "branch": "main", "lock": "app-main"}],
    "test_repo": {"path": "/repo/app-tests", "module": "baidu/team/app-tests", "branch": "main", "lock": "tests-main"},
    "language_skill": "/skills/language",
    "project_skill": "/skills/project",
    "knowledge_sources": [{
        "provider": "ku", "repository": "knowledge", "revision": "r1",
        "search_scope": "project", "priority": 1, "repo_id": "repo-1", "parent_doc_id": "parent-1",
    }],
    "review_provider": {"kind": "source-only", "command": "review"},
    "pipeline_profile": {
        "pipeline_id": "pipe-1",
        "allowed_parameters": ["mode"],
        "stage_classes": ["compile", "unit", "release"],
        "release_rule": "manual-approval",
    },
    "environment_profile": {
        "runner": "linux", "os_arch": "linux/amd64", "image_digest": "sha256:env",
        "toolchain_digest": "sha256:tools", "hardware_or_simulator": "remote",
        "data": "fixture", "services": "none", "capacity": "small",
    },
    "approval_channels": {
        "comate": {"channel": "comate-review"}, "infoflow": {"channel": "group-1"},
        "role_members": {
            "development": ["dev@example.test"], "test": ["test@example.test"],
            "project": ["owner@example.test"],
        },
    },
}

REVISIONS = {
    "run_id": "run-1",
    "revision_set_id": "revset-1",
    "repositories": [
        {"kind": "business", "module": "baidu/team/app", "revision": "app-rev", "branch": "main"},
        {"kind": "test", "module": "baidu/team/app-tests", "revision": "test-rev", "branch": "main"},
    ],
    "parameters": {"mode": "remote"},
}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment_fingerprint(profile=PROFILE):
    return canonical_hash(profile["environment_profile"])


def trigger_binding(profile=PROFILE, revisions=REVISIONS):
    return {
        "operation": "ipipe.trigger",
        "run_id": revisions["run_id"],
        "pipeline_id": profile["pipeline_profile"]["pipeline_id"],
        "module": profile["business_repos"][0]["module"],
        "revision_set_id": revisions["revision_set_id"],
        "repositories": revisions["repositories"],
        "environment_fingerprint": environment_fingerprint(profile),
        "parameters": revisions.get("parameters", {}),
    }


def rerun_binding(failure_signature, profile=PROFILE, revisions=REVISIONS):
    return {
        "operation": "ipipe.rerun",
        "run_id": revisions["run_id"],
        "pipeline_id": profile["pipeline_profile"]["pipeline_id"],
        "module": profile["business_repos"][0]["module"],
        "environment_fingerprint": environment_fingerprint(profile),
        "build_id": "build-1",
        "stage_build_id": "stage-1",
        "revision_set_id": revisions["revision_set_id"],
        "repositories": revisions["repositories"],
        "failure_signature": failure_signature,
    }


def approved(ledger, action, input_hash, run_id="run-1"):
    policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
    row = ledger.request(action, input_hash, ["comate", "infoflow"], run_id=run_id, member_policy=policy)
    for channel in ("comate", "infoflow"):
        ledger.record_delivery(row["approval_id"], channel, {"request_id": channel}, payload_hash=input_hash)
    ledger.resolve(row["approval_id"], "APPROVE", input_hash, "comate", run_id=run_id, responder="owner@example.test")
    return {"approval_id": row["approval_id"], "input_hash": input_hash}


def pinned_runtime(state, ledger, api, **options):
    return IpipeRuntime(
        state,
        ledger,
        "run-1",
        api,
        validated_profile=json.loads(json.dumps(PROFILE)),
        profile_hash=canonical_hash(PROFILE),
        **options,
    )


def build(build_id="build-1", status="RUNNING", stages=None):
    return {
        "id": build_id,
        "pipelineConfId": "pipe-1",
        "module": "baidu/team/app",
        "revision": "app-rev",
        "revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "test-rev"},
        "params": {"mode": "remote"},
        "status": status,
        "stageBuilds": stages or [],
    }


class FakeApi:
    def __init__(self):
        self.pipeline = {"id": "pipe-1", "module": "baidu/team/app"}
        self.candidates = []
        self.builds = {}
        self.stages = {}
        self.jobs = {}
        self.details = {}
        self.releases = []
        self.calls = []
        self.trigger_result = {"id": "build-1"}
        self.rerun_result = {"stageBuildId": "stage-1", "status": "RUNNING"}
        self.on_trigger = None
        self.on_rerun = None

    def get_pipeline_by_id(self, pipeline_id, brief=True):
        self.calls.append(("get_pipeline_by_id", pipeline_id))
        return self.pipeline

    def builds_by_revision(self, module, revision, pipeline_id):
        self.calls.append(("builds_by_revision", module, revision, pipeline_id))
        return list(self.candidates)

    # Like the platform, a stage that is executing (or has executed) reports when it
    # started. A fixture stage that omits `startTime` is stamped the first time it is
    # read in that status, and keeps the stamp while the status stays the same, so a
    # stage that was already SUCCESS/RUNNING before a rerun intent reads as old while a
    # status change after the POST reads as a new start.
    _STAMPED_STATUSES = {"RUNNING", "SKIPPING", "BUILDING", "EXECUTING", "SUCC", "SUCCESS", "SUCCEEDED"}

    def _stamp(self, stages):
        if not hasattr(self, "_start_stamps"):
            self._start_stamps = {}
        stamped = []
        for stage in stages or []:
            item = dict(stage)
            status = str(item.get("status") or "").upper()
            if "startTime" not in item and status in self._STAMPED_STATUSES:
                key = (str(item.get("id") or item.get("stageBuildId") or ""), status)
                if key not in self._start_stamps:
                    self._start_stamps[key] = int(time.time() * 1000)
                item["startTime"] = self._start_stamps[key]
            stamped.append(item)
        return stamped

    def build_by_id(self, build_id, *, module=None, revision=None, pipeline_id=None):
        self.calls.append(("build_by_id", build_id))
        record = dict(self.builds[build_id])
        if isinstance(record.get("stageBuilds"), list):
            record["stageBuilds"] = self._stamp(record["stageBuilds"])
        return record

    def pipeline_stage_info(self, build_id):
        self.calls.append(("pipeline_stage_info", build_id))
        return self._stamp(self.stages.get(build_id, []))

    def failed_jobs(self, build_id):
        self.calls.append(("failed_jobs", build_id))
        return [dict(job) for job in self.jobs.get(build_id, [])]

    def stage_detail(self, stage_id):
        self.calls.append(("stage_detail", stage_id))
        return self.details.get(stage_id, {})

    def release_info(self, module, branch):
        self.calls.append(("release_info", module, branch))
        return list(self.releases)

    def trigger_by_revision(self, pipeline_id, revision, parameters):
        self.calls.append(("trigger_by_revision", pipeline_id, revision, dict(parameters)))
        if self.on_trigger:
            self.on_trigger()
        if isinstance(self.trigger_result, Exception):
            raise self.trigger_result
        return dict(self.trigger_result)

    def manual_execute_stage(self, stage_id, parameters):
        self.calls.append(("manual_execute_stage", stage_id, dict(parameters)))
        if self.on_rerun:
            self.on_rerun()
        if isinstance(self.rerun_result, Exception):
            raise self.rerun_result
        return dict(self.rerun_result)


class IpipeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")
        self.api = FakeApi()
        self.runtime = pinned_runtime(self.state, self.ledger, self.api, sleeper=lambda _seconds: None)

    def tearDown(self):
        self.temp.cleanup()

    def bind_build(self, status="RUNNING", stages=None):
        item = build(status=status, stages=stages)
        self.api.candidates = [item]
        self.api.builds["build-1"] = item
        result = self.runtime.discover(PROFILE, REVISIONS)
        self.assertEqual(result["reason_code"], "OK")

    def test_runtime_accepts_schema_valid_profile_and_revision_parameter_payload(self):
        self.assertEqual(validate_profile(PROFILE, check_paths=False)["reason_code"], "READY")
        self.api.candidates = [build()]
        result = self.runtime.discover(PROFILE, REVISIONS)
        self.assertEqual(result["reason_code"], "OK")

    def test_runtime_rejects_profile_different_from_its_pinned_validated_profile(self):
        self.runtime.validated_profile = json.loads(json.dumps(PROFILE))
        self.runtime.profile_hash = canonical_hash(PROFILE)
        changed = json.loads(json.dumps(PROFILE))
        changed["pipeline_profile"]["pipeline_id"] = "other-pipeline"
        self.api.pipeline = {"id": "other-pipeline", "module": "baidu/team/app"}
        candidate = build()
        candidate["pipelineConfId"] = "other-pipeline"
        self.api.candidates = [candidate]
        result = self.runtime.discover(changed, REVISIONS)
        self.assertEqual(result["reason_code"], "PROFILE_CONFLICT")

    def test_discover_requires_exact_single_pipeline_module_and_full_revision_set(self):
        wrong = build("wrong")
        wrong["revisions"]["baidu/team/app-tests"] = "stale"
        self.api.candidates = [wrong]
        none = self.runtime.discover(PROFILE, REVISIONS)
        self.api.candidates = [build("a"), build("b")]
        many = self.runtime.discover(PROFILE, REVISIONS)
        self.api.candidates = [build()]
        exact = self.runtime.discover(PROFILE, REVISIONS)
        self.assertEqual(none["reason_code"], "BUILD_QUERY_REQUIRED")
        self.assertEqual(many["reason_code"], "BUILD_AMBIGUOUS")
        self.assertEqual(exact["build_id"], "build-1")

    def test_trigger_rejects_forbidden_parameter_before_approval_or_write(self):
        revisions = json.loads(json.dumps(REVISIONS))
        revisions["parameters"]["shell"] = "blocked-command"
        result = self.runtime.trigger(PROFILE, revisions, {"approval_id": "none", "input_hash": "x"})
        self.assertEqual(result["reason_code"], "PIPELINE_PARAMETER_FORBIDDEN")
        self.assertFalse(any(call[0] == "trigger_by_revision" for call in self.api.calls))

    def test_trigger_rejects_secret_material_even_when_parameter_name_is_allowlisted(self):
        revisions = json.loads(json.dumps(REVISIONS))
        revisions["parameters"] = {"mode": "Bearer-must-not-persist"}
        result = self.runtime.trigger(PROFILE, revisions, {"approval_id": "none", "input_hash": "x"})
        self.assertEqual(result["reason_code"], "PIPELINE_PARAMETER_FORBIDDEN")
        self.assertFalse(any(call[0] == "trigger_by_revision" for call in self.api.calls))

    def test_trigger_requires_exact_run_bound_g7_hash(self):
        input_hash = canonical_hash(trigger_binding())
        wrong_gate = approved(self.ledger, "G8", input_hash)
        wrong_hash = approved(self.ledger, "G7", "0" * 64)
        first = self.runtime.trigger(PROFILE, REVISIONS, wrong_gate)
        second = self.runtime.trigger(PROFILE, REVISIONS, wrong_hash)
        self.assertEqual(first["reason_code"], "APPROVAL_GATE_MISMATCH")
        self.assertEqual(second["reason_code"], "APPROVAL_INPUT_MISMATCH")
        self.assertFalse(any(call[0] == "trigger_by_revision" for call in self.api.calls))

    def test_trigger_verifies_response_and_unknown_result_is_never_replayed(self):
        approval = approved(self.ledger, "G7", canonical_hash(trigger_binding()))
        self.api.trigger_result = TimeoutError("unknown")
        first = self.runtime.trigger(PROFILE, REVISIONS, approval)
        second = self.runtime.trigger(PROFILE, REVISIONS, approval)
        self.assertEqual(first["reason_code"], "TRIGGER_RESULT_UNKNOWN")
        self.assertEqual(second["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in self.api.calls), 1)

    def test_trigger_never_posts_after_any_nonconfirmed_discovery_result(self):
        for index, reason in enumerate((
            "PIPELINE_QUERY_FAILED",
            "PIPELINE_IDENTITY_MISMATCH",
            "BUILD_QUERY_FAILED",
            "BUILD_AMBIGUOUS",
        )):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                state = StateStore(Path(directory) / "state.sqlite")
                ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
                api = FakeApi()
                runtime = pinned_runtime(state, ledger, api)
                runtime.discover = lambda _profile, _revisions, _module=None, reason=reason: {"ok": False, "reason_code": reason}
                approval = approved(ledger, "G7", canonical_hash(trigger_binding()))

                result = runtime.trigger(PROFILE, REVISIONS, approval)

                self.assertEqual(result["reason_code"], reason)
                self.assertFalse(any(call[0] == "trigger_by_revision" for call in api.calls))

    def test_discovery_resolves_the_pipeline_registered_for_the_named_module(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["business_repos"].append(
            {"path": "/repo/agent", "module": "baidu/team/agent", "branch": "main", "lock": "agent-main"}
        )
        profile["pipeline_profile"]["pipelines"] = [
            {
                "module": "baidu/team/app", "pipeline_id": "pipe-1",
                "stage_classes": ["compile", "unit"], "required_for_release": True,
            },
            {
                "module": "baidu/team/agent", "pipeline_id": "pipe-2",
                "stage_classes": ["compile", "unit"], "required_for_release": True,
            },
        ]
        revisions = json.loads(json.dumps(REVISIONS))
        revisions["repositories"].insert(
            1, {"kind": "business", "module": "baidu/team/agent", "revision": "agent-rev", "branch": "main"}
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            api = FakeApi()
            api.pipeline = {"id": "pipe-2", "module": "baidu/team/agent"}
            runtime = IpipeRuntime(
                state, ledger, "run-1", api,
                validated_profile=json.loads(json.dumps(profile)),
                profile_hash=canonical_hash(profile),
            )

            result = runtime.discover(profile, revisions, "baidu/team/agent")

            self.assertEqual(result["reason_code"], "BUILD_QUERY_REQUIRED", result)
            self.assertIn(("get_pipeline_by_id", "pipe-2"), api.calls)
            self.assertIn(("builds_by_revision", "baidu/team/agent", "agent-rev", "pipe-2"), api.calls)
            unknown = runtime.discover(profile, revisions, "baidu/team/absent")
            self.assertEqual(unknown["reason_code"], "PIPELINE_IDENTITY_MISMATCH", unknown)

    def test_registered_module_release_rule_overrides_profile_default(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["pipeline_profile"]["pipelines"] = [{
            "module": "baidu/team/app", "pipeline_id": "pipe-1",
            "stage_classes": ["compile", "unit"], "required_for_release": True,
            "release_rule": "app-specific",
        }]
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
            runtime = IpipeRuntime(
                state, ledger, "run-1", FakeApi(),
                validated_profile=json.loads(json.dumps(profile)),
                profile_hash=canonical_hash(profile),
            )
            context = runtime._context(profile, REVISIONS)
            self.assertEqual(context["release_rule"], "app-specific")

    def test_typed_transport_failure_is_preserved_by_discovery(self):
        def denied(_pipeline_id):
            raise IpipeTransportError("AUTH_REQUIRED", status=401, transient=False)

        self.api.get_pipeline_by_id = denied
        result = self.runtime.discover(PROFILE, REVISIONS)
        self.assertEqual(result["reason_code"], "AUTH_REQUIRED")

    def test_concurrent_trigger_allows_one_writer_then_replays_receipt(self):
        approval = approved(self.ledger, "G7", canonical_hash(trigger_binding()))
        entered, release = threading.Event(), threading.Event()

        def finish():
            entered.set()
            release.wait(2)
            self.api.builds["build-1"] = build()

        self.api.on_trigger = finish
        results = []
        thread = threading.Thread(target=lambda: results.append(self.runtime.trigger(PROFILE, REVISIONS, approval)))
        thread.start()
        self.assertTrue(entered.wait(2))
        non_owner = pinned_runtime(self.state, self.ledger, self.api).trigger(PROFILE, REVISIONS, approval)
        release.set()
        thread.join(2)
        replay = pinned_runtime(self.state, self.ledger, self.api).trigger(PROFILE, REVISIONS, approval)
        self.assertEqual(non_owner["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(results[0]["reason_code"], "OK")
        self.assertEqual(replay["build_id"], "build-1")
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in self.api.calls), 1)

    def test_monitor_checks_stage_failure_before_successful_aggregate_and_bounds_logs(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="SUCCESS", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.api.jobs["build-1"] = [{"id": "job-1", "status": "FAIL", "message": "assertion failed " + "x" * 9000}]
        self.api.details["stage-1"] = {"log": "y" * 9000}
        result = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual(result["status"], "FAILURE")
        self.assertEqual(result["classification"], "TEST_FAILURE")
        self.assertLessEqual(len(result["log_excerpt"]), 4096)
        self.assertIn("ipipe:job/job-1", result["evidence_refs"])
        self.assertIn("ipipe:module-stage/baidu_team_app-build-1-stage-1", result["evidence_refs"])
        self.assertIn("ipipe:module-revision/baidu_team_app-app-rev", result["evidence_refs"])
        names = [call[0] for call in self.api.calls]
        self.assertLess(names.index("pipeline_stage_info"), names.index("failed_jobs"))

    def test_monitor_redacts_authorization_material_from_failure_excerpt(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.api.jobs["build-1"] = [{
            "id": "job-1", "status": "FAIL",
            "message": "x-ac-Authorization: Bearer-sensitive-value\nassertion failed",
        }]
        result = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertNotIn("sensitive-value", result["log_excerpt"])
        self.assertIn("[REDACTED]", result["log_excerpt"])

    def test_monitor_distinguishes_manual_wait_timeout_and_revision_mismatch(self):
        waiting = {"id": "stage-1", "stageName": "release", "status": "WAITING_FOR_MANUAL"}
        self.bind_build(status="RUNNING", stages=[waiting])
        self.api.stages["build-1"] = [waiting]
        manual = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual(manual["status"], "MANUAL_WAIT")
        self.api.stages["build-1"] = [{"id": "stage-1", "stageName": "unit", "status": "RUNNING"}]
        timeout = self.runtime.monitor("build-1", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.assertEqual(timeout["status"], "TIMEOUT")
        self.api.builds["build-1"]["revision"] = "other"
        mismatch = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual(mismatch["reason_code"], "REVISION_MISMATCH")

    def _expression_skip_fixture(self, skip_reason="EXPRESSION", aggregate="SKIPPED"):
        ran = {"id": "stage-1", "stageName": "unit", "status": "SUCC",
               "jobBuildBeans": [{"id": "job-1", "stageBuildId": "stage-1", "status": "SUCC"}]}
        skipped = {"id": "stage-2", "stageName": "system", "status": "SKIPPED",
                   "jobBuildBeans": [{"id": "job-2", "stageBuildId": "stage-2", "status": "SKIPPED"}]}
        self.bind_build(status=aggregate, stages=[ran, skipped])
        self.api.stages["build-1"] = [ran, skipped]
        self.api.details["stage-2"] = {"entities": {"realJobBuilds": [[
            {"id": 2, "jobName": "system", "status": "SKIPPED", "skipByStage": skip_reason}]]}}
        return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    def test_a_stage_the_pipeline_expression_skipped_is_not_missing_evidence(self):
        # BGW 348102 skips its system-test stage for every dpdk-2011 branch; the build then
        # reports SKIPPED although every stage that could run passed.
        result = self.runtime.monitor("build-1", self._expression_skip_fixture())
        self.assertEqual(result["status"], "SUCCESS")

    def test_a_manually_skipped_stage_still_blocks_success(self):
        result = self.runtime.monitor_once(
            "build-1", self._expression_skip_fixture(skip_reason="MANUAL"))
        self.assertEqual(result["status"], "MONITORING")

    def test_a_build_whose_every_stage_was_expression_skipped_is_not_success(self):
        skipped = {"id": "stage-2", "stageName": "system", "status": "SKIPPED",
                   "jobBuildBeans": [{"id": "job-2", "stageBuildId": "stage-2", "status": "SKIPPED"}]}
        self.bind_build(status="SKIPPED", stages=[skipped])
        self.api.stages["build-1"] = [skipped]
        self.api.details["stage-2"] = {"entities": {"realJobBuilds": [[
            {"id": 2, "status": "SKIPPED", "skipByStage": "EXPRESSION"}]]}}
        result = self.runtime.monitor_once(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual(result["status"], "MONITORING")

    def test_monitor_once_returns_checkpoint_for_running_build(self):
        self.bind_build(
            status="RUNNING",
            stages=[{"id": "stage-1", "stageName": "unit", "status": "RUNNING"}],
        )
        self.api.stages["build-1"] = [
            {"id": "stage-1", "stageName": "unit", "status": "RUNNING"}
        ]

        result = self.runtime.monitor_once(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason_code"], "MONITORING")
        self.assertEqual(result["status"], "MONITORING")
        self.assertEqual(result["build_id"], "build-1")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "build_by_id"]), 1)

    def test_monitor_rejects_wrong_build_identity_empty_stages_and_missing_stage_id(self):
        self.bind_build(status="SUCCESS")
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.api.builds["build-1"] = build("different-build", status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        wrong_build = self.runtime.monitor("build-1", deadline)
        self.api.builds["build-1"] = build(status="SUCCESS")
        self.api.stages["build-1"] = []
        empty = self.runtime.monitor("build-1", deadline)
        self.api.stages["build-1"] = [{"stageName": "unit", "status": "SUCCESS"}]
        anonymous = self.runtime.monitor("build-1", deadline)
        self.api.stages["build-1"] = [
            {"id": "stage-1", "stageName": "unit", "status": "SUCCESS"},
            {"id": "stage-1", "stageName": "release", "status": "SUCCESS"},
        ]
        duplicate = self.runtime.monitor("build-1", deadline)
        self.assertEqual(wrong_build["reason_code"], "BUILD_IDENTITY_MISMATCH")
        self.assertEqual(empty["reason_code"], "STAGE_IDENTITY_MISSING")
        self.assertEqual(anonymous["reason_code"], "STAGE_IDENTITY_MISSING")
        self.assertEqual(duplicate["reason_code"], "STAGE_IDENTITY_MISMATCH")

    def test_stage_permission_failure_is_not_hidden_by_embedded_stage_fallback(self):
        self.bind_build(status="SUCCESS", stages=[{"id": "stage-1", "status": "SUCCESS"}])
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        self.api.pipeline_stage_info = lambda _build_id: (_ for _ in ()).throw(
            IpipeTransportError("PERMISSION_DENIED", status=403, transient=False)
        )

        result = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

        self.assertEqual(result["reason_code"], "PERMISSION_DENIED")

    def test_rerun_requires_owned_failed_stage_exact_g8_and_has_durable_budget(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "RUNNING"}]
        first = self.runtime.rerun("stage-1", approval)
        replay = pinned_runtime(self.state, self.ledger, self.api).rerun("stage-1", approval)
        changed = dict(approval, input_hash="changed")
        conflict = self.runtime.rerun("stage-1", changed)
        forged = self.runtime.rerun(
            "stage-1", {"approval_id": "forged-approval", "input_hash": approval["input_hash"]}
        )
        self.assertEqual(first["reason_code"], "OK")
        self.assertEqual(replay["stage_build_id"], "stage-1")
        self.assertEqual(conflict["reason_code"], "RERUN_CONFLICT")
        self.assertEqual(forged["reason_code"], "APPROVAL_REQUIRED")
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_a_log_link_missing_from_the_listing_is_taken_from_the_stage_detail(self):
        # `pipeline_stage_info` carries job statuses but no log links, so a stage bound
        # from the listing used to have nothing to read and its silent failure vanished.
        stage = _normalize_stage({
            "id": 637340540, "stageName": "P0新case回归", "status": "SUCC",
            "jobBuildBeans": [{"id": 1018370712, "jobName": "P0新功能调试", "status": "SUCC"}],
        })
        self.assertEqual(stage["jobs"][0]["log_url"], "")
        api = FakeApi()
        api.details["637340540"] = {"entities": {"realJobBuilds": [[{
            "id": 1018370712, "logs": [{"url": "https://logonline.example/detail"}],
        }]]}}
        read = []

        def reader(url):
            read.append(url)
            return {"ok": True, "success_ratios": [0.0], "failed_cases": ["case-1"]}

        runtime = pinned_runtime(self.state, self.ledger, api, log_reader=reader)
        found = runtime._silent_case_failures([stage])

        self.assertEqual(read, ["https://logonline.example/detail"])
        self.assertEqual(found[0]["log_url"], "https://logonline.example/detail")
        self.assertEqual(found[0]["failed_cases"], ["case-1"])

    def test_reexecuting_a_finished_stage_accepts_the_new_stage_build_it_allocates(self):
        # iPipe does not re-use the stage build id when a completed stage is executed
        # again: it allocates a new one for the same stage of the same build. Demanding
        # the requested id back reported a stage that had genuinely started as an invalid
        # response, and left the intent without a receipt.
        finished = {
            "id": "stage-1", "stageConfId": 4875670, "stageName": "P0新case回归", "status": "SUCC",
            "jobBuildBeans": [{
                "id": "job-1", "jobName": "P0新功能调试", "status": "SUCC",
                "logs": [{"url": "https://logonline.example/silent"}],
            }],
        }
        successor = {
            "id": "stage-2", "stageConfId": 4875670, "stageName": "P0新case回归", "status": "RUNNING",
            "jobBuildBeans": [{"id": "job-2", "jobName": "P0新功能调试", "status": "RUNNING"}],
        }
        self.bind_build(status="SUCCESS", stages=[finished])
        self.api.stages["build-1"] = [finished]
        runtime = pinned_runtime(
            self.state, self.ledger, self.api,
            log_reader=lambda url: {"ok": True, "success_ratios": [0.0], "failed_cases": ["case-1"]},
        )
        runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", runtime.rerun_input_hash("stage-1")["input_hash"])
        self.api.rerun_result = {"stageBuildId": "stage-2", "status": "RUNNING"}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[finished, successor])
        self.api.stages["build-1"] = [finished, successor]

        executed = runtime.rerun("stage-1", approval)

        self.assertEqual(executed["reason_code"], "OK")
        self.assertEqual(executed["stage_build_id"], "stage-1")
        self.assertEqual(executed["started_stage_build_id"], "stage-2")
        self.assertIn("ipipe:stage/stage-2", executed["evidence_refs"])
        # The new run is owned too, so monitoring and a later re-run can address it.
        self.assertIsNotNone(
            self.state.idempotency_result("ipipe.stage-binding:run-1:stage-2")
        )

    def test_a_response_naming_a_different_stage_is_still_refused(self):
        failed = {"id": "stage-1", "stageConfId": 1, "stageName": "unit", "status": "FAIL"}
        unrelated = {"id": "stage-9", "stageConfId": 2, "stageName": "release", "status": "RUNNING"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        approval = approved(
            self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"]))
        )
        self.api.rerun_result = {"stageBuildId": "stage-9", "status": "RUNNING"}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[failed, unrelated])
        self.api.stages["build-1"] = [failed, unrelated]

        result = self.runtime.rerun("stage-1", approval)

        self.assertEqual(result["reason_code"], "RERUN_RESPONSE_INVALID")

    def test_reconciliation_works_against_a_build_record_that_states_no_revision_map(self):
        # The real gateway returns no per-repository revision map, so comparing one made
        # every unknown re-run result unreconcilable and parked the run for good.
        failed = {"id": "stage-1", "stageConfId": 7, "stageName": "unit", "status": "FAIL"}
        successor = {"id": "stage-2", "stageConfId": 7, "stageName": "unit", "status": "RUNNING"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        approval = approved(
            self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"]))
        )
        self.api.rerun_result = TimeoutError("unknown")
        unknown = self.runtime.rerun("stage-1", approval)
        without_map = build(status="RUNNING", stages=[failed, successor])
        without_map.pop("revisions")
        self.api.builds["build-1"] = without_map
        self.api.stages["build-1"] = [failed, successor]

        reconciled = pinned_runtime(self.state, self.ledger, self.api).rerun("stage-1", approval)

        self.assertEqual(unknown["reason_code"], "RERUN_RESULT_UNKNOWN")
        self.assertEqual(reconciled["reason_code"], "OK")
        self.assertEqual(reconciled["started_stage_build_id"], "stage-2")
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_a_stage_that_reported_success_over_failed_cases_stays_rerunnable(self):
        succeeded = {
            "id": "stage-1", "stageName": "P0新case回归", "status": "SUCC",
            "jobBuildBeans": [{
                "id": "job-1", "jobName": "P0新功能调试", "status": "SUCC",
                "logs": [{"url": "https://logonline.example/silent"}],
            }],
        }
        self.bind_build(status="SUCCESS", stages=[succeeded])
        self.api.stages["build-1"] = [succeeded]
        silent = {"ok": True, "success_ratios": [0.0], "failed_cases": ["test_stun_route_NAT44_cc_tcp_017"]}
        runtime = pinned_runtime(self.state, self.ledger, self.api, log_reader=lambda url: silent)
        runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

        bound = runtime.rerun_input_hash("stage-1")
        approval = approved(self.ledger, "G8", bound["input_hash"])
        executed = runtime.rerun("stage-1", approval)

        self.assertEqual(bound["reason_code"], "OK")
        self.assertEqual(bound["case_failures"][0]["job_build_id"], "job-1")
        self.assertEqual(executed["reason_code"], "OK")
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_a_stage_whose_cases_all_passed_is_still_refused(self):
        succeeded = {
            "id": "stage-1", "stageName": "unit", "status": "SUCC",
            "jobBuildBeans": [{
                "id": "job-1", "jobName": "unit", "status": "SUCC",
                "logs": [{"url": "https://logonline.example/ok"}],
            }],
        }
        self.bind_build(status="SUCCESS", stages=[succeeded])
        self.api.stages["build-1"] = [succeeded]
        runtime = pinned_runtime(
            self.state, self.ledger, self.api,
            log_reader=lambda url: {"ok": True, "success_ratios": [100.0], "failed_cases": []},
        )
        runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

        self.assertEqual(
            runtime.rerun_input_hash("stage-1")["reason_code"], "STAGE_NOT_RERUNNABLE"
        )
        self.assertEqual(
            runtime.rerun("stage-1", {"approval_id": "a", "input_hash": "b"})["reason_code"],
            "STAGE_NOT_RERUNNABLE",
        )
        self.assertFalse(any(call[0] == "manual_execute_stage" for call in self.api.calls))

    def test_the_printed_g8_hash_is_the_one_the_write_demands(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

        printed = self.runtime.rerun_input_hash("stage-1")
        with_parameters = self.runtime.rerun_input_hash(
            "stage-1", parameters={"test_cr_id": "122402145"}
        )
        approval = approved(self.ledger, "G8", printed["input_hash"])
        self.api.builds["build-1"] = build(
            status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}]
        )
        executed = self.runtime.rerun("stage-1", approval)

        self.assertEqual(printed["input_hash"], canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.assertNotEqual(with_parameters["input_hash"], printed["input_hash"])
        self.assertEqual(executed["reason_code"], "OK")

    def test_ack_without_stage_identity_is_confirmed_by_get_and_never_reposted(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "PENDING_FOR_USER"}
        self.bind_build(status="RUNNING", stages=[failed])
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        hashed = self.runtime.rerun_input_hash("stage-1")
        approval = approved(self.ledger, "G8", hashed["input_hash"])
        self.api.rerun_result = {"accepted": True}
        self.api.on_rerun = lambda: self.api.builds["build-1"].update(
            stageBuilds=[{"id": "stage-1", "stageName": "unit", "status": "SKIPPING"}])
        result = self.runtime.rerun("stage-1", approval)
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.runtime.reconcile_rerun("stage-1", approval)["ok"])
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)
        observations = self.state.external_observations("run-1")
        self.assertEqual(observations[-1]["observation"]["response_keys"], ["accepted"])

    def test_an_identityless_ack_is_confirmed_once_the_stage_shows_its_start(self):
        # iPipe acknowledges a manual-stage execute with {code, entities, msg}; the stage
        # listing reports the start a few seconds later (BGW-1995 on 348101 and 348102).
        manual = {"id": "stage-1", "stageName": "unit", "status": "PENDING_FOR_USER"}
        self.bind_build(status="RUNNING", stages=[manual])
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        self.api.rerun_result = {"code": 0, "entities": None, "msg": "ok"}
        polls = []

        def later_start(_seconds):
            polls.append(_seconds)
            self.api.builds["build-1"].update(
                stageBuilds=[{"id": "stage-1", "stageName": "unit", "status": "RUNNING"}])
        self.runtime.sleeper = later_start
        self.api.on_rerun = lambda: self.api.builds["build-1"].update(
            stageBuilds=[{"id": "stage-1", "stageName": "unit", "status": "WAITTING"}])
        result = self.runtime.rerun("stage-1", approval)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(polls), 1)
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_waiting_after_ack_stays_unknown_until_controlled_get_recovery(self):
        manual = {"id": "stage-1", "stageName": "unit", "status": "PENDING_FOR_USER"}
        self.bind_build(status="RUNNING", stages=[manual])
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        self.api.rerun_result = {}
        self.api.on_rerun = lambda: self.api.builds["build-1"].update(
            stageBuilds=[{"id": "stage-1", "stageName": "unit", "status": "WAITTING"}])
        self.assertEqual(self.runtime.rerun("stage-1", approval)["reason_code"], "RERUN_RESPONSE_INVALID")
        self.api.builds["build-1"]["stageBuilds"][0]["status"] = "RUNNING"
        self.assertEqual(self.runtime.reconcile_rerun(
            "stage-1", {**approval, "input_hash": "wrong"})["reason_code"], "RERUN_CONFLICT")
        self.assertTrue(self.runtime.reconcile_rerun("stage-1", approval)["ok"])
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_a_manual_stage_behind_a_running_stage_gets_no_g8(self):
        # 348101 lists 发布 as PENDING_FOR_USER while 自动化回归测试 still runs (BGW-1995).
        running = {"id": "stage-0", "stageName": "regression", "status": "RUNNING"}
        release = {"id": "stage-1", "stageName": "release", "status": "PENDING_FOR_USER"}
        self.bind_build(status="RUNNING", stages=[running, release])
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        early = self.runtime.rerun_input_hash("stage-1")
        self.assertEqual(early["reason_code"], "MANUAL_STAGE_NOT_READY")
        self.assertEqual(early["waiting_on"][0]["stage_build_id"], "stage-0")

    def test_an_approval_granted_before_the_stage_was_ready_does_not_run_it(self):
        running = {"id": "stage-0", "stageName": "regression", "status": "RUNNING"}
        release = {"id": "stage-1", "stageName": "release", "status": "PENDING_FOR_USER"}
        self.bind_build(status="RUNNING", stages=[running, release])
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        # The hash an older controller asked for: the binding without readiness facts.
        from clients.ipipe_runtime import _canonical_hash
        self.api.builds["build-1"]["stageBuilds"] = [
            {**running, "status": "SUCC"}, release]
        ready = self.runtime.rerun_input_hash("stage-1")
        self.assertEqual(ready["reason_code"], "OK")
        legacy = {k: v for k, v in ready["binding"].items() if k != "ready_after"}
        wrong = approved(self.ledger, "G8", _canonical_hash(legacy))
        self.assertFalse(self.runtime.rerun("stage-1", wrong)["ok"])
        self.assertFalse(any(c[0] == "manual_execute_stage" for c in self.api.calls))
        right = approved(self.ledger, "G8", ready["input_hash"])
        self.runtime.rerun("stage-1", right)
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_rerun_response_parser_accepts_nested_stage_but_not_boolean_id(self):
        from clients.ipipe_runtime import _rerun_response_stage
        self.assertEqual(_rerun_response_stage(
            {"entities": {"stageBuild": {"stageBuildId": 123}}}), "123")
        for response in ({}, {"id": True}, {"entities": True}, True):
            self.assertEqual(_rerun_response_stage(response), "")

    def test_failed_stage_recovery_requires_start_after_claim(self):
        from clients.ipipe_runtime import _rerun_started
        self.assertFalse(_rerun_started({"status": "FAIL", "start_time": 1000},
                                       since="2026-10-12T09:52:49.830544+00:00"))
        self.assertTrue(_rerun_started({"status": "FAIL", "start_time": 1791798770000},
                                      since="2026-10-12T09:52:49.830544+00:00"))

    def test_a_status_without_a_new_start_time_never_proves_a_rerun_started(self):
        # A-01: SUCCESS/RUNNING alone is not evidence; the start must postdate the intent.
        from clients.ipipe_runtime import _rerun_started
        since = "2026-10-12T09:52:49+00:00"
        claimed_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        for status in ("SUCC", "RUNNING"):
            with self.subTest(status=status):
                self.assertFalse(_rerun_started({"status": status}, since=since))
                self.assertFalse(_rerun_started(
                    {"status": status, "start_time": claimed_ms - 3600_000}, since=since))
                self.assertTrue(_rerun_started(
                    {"status": status, "start_time": claimed_ms + 5_000}, since=since))

    def test_a_stage_already_successful_before_the_intent_is_not_receipted(self):
        # A-01 trigger 1: a silent-case SUCC stage is re-run; the POST times out and the
        # stage still reads SUCC with its old start time. That is the old run, not this one.
        old_start = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp() * 1000)
        succeeded = {
            "id": "stage-1", "stageName": "P0新case回归", "status": "SUCC", "startTime": old_start,
            "jobBuildBeans": [{"id": "job-1", "jobName": "P0新功能调试", "status": "SUCC",
                               "logs": [{"url": "https://logonline.example/silent"}]}],
        }
        self.bind_build(status="SUCCESS", stages=[succeeded])
        self.api.stages["build-1"] = [succeeded]
        runtime = pinned_runtime(
            self.state, self.ledger, self.api,
            log_reader=lambda url: {"ok": True, "success_ratios": [0.0], "failed_cases": ["c"]},
        )
        runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", runtime.rerun_input_hash("stage-1")["input_hash"])
        self.api.rerun_result = TimeoutError("unknown")

        first = runtime.rerun("stage-1", approval)
        again = runtime.reconcile_rerun("stage-1", approval)

        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN", first)
        self.assertEqual(again["reason_code"], "QUERY_REQUIRED", again)
        self.assertIsNone(self.state.result_by_idempotency_key(
            f"ipipe.rerun:run-1:stage-1:{approval['approval_id']}"))
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_a_successor_receipted_by_an_earlier_attempt_is_not_this_attempts_evidence(self):
        # A-01 trigger 2: attempt 1 re-executed stage-1 into stage-2 and was receipted.
        # Attempt 2's POST is lost; stage-2 is still RUNNING. It belongs to attempt 1.
        failed = {"id": "stage-1", "stageConfId": 7, "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        first_approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        successor = {"id": "stage-2", "stageConfId": 7, "stageName": "unit", "status": "RUNNING",
                     "startTime": int(time.time() * 1000) + 1000}
        self.api.rerun_result = {"stageBuildId": "stage-2", "status": "RUNNING"}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[failed, successor])
        self.api.stages["build-1"] = [failed, successor]
        first = self.runtime.rerun("stage-1", first_approval)
        self.assertEqual(first["started_stage_build_id"], "stage-2", first)

        options = {"parameters": {"k": "v"}}
        second_approval = approved(
            self.ledger, "G8", self.runtime.rerun_input_hash("stage-1", **options)["input_hash"])
        self.api.rerun_result = TimeoutError("unknown")
        second = self.runtime.rerun("stage-1", second_approval, **options)
        settled = self.runtime.reconcile_rerun("stage-1", second_approval)

        self.assertEqual(second["reason_code"], "RERUN_RESULT_UNKNOWN", second)
        self.assertEqual(settled["reason_code"], "QUERY_REQUIRED", settled)
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 2)

    def test_rediscovering_an_owned_build_keeps_the_stored_binding(self):
        self.bind_build(status="RUNNING")
        stored = self.state.idempotency_result("ipipe.build-binding:run-1:build-1")

        again = self.runtime.discover(PROFILE, REVISIONS)
        restarted = pinned_runtime(self.state, self.ledger, self.api).discover(PROFILE, REVISIONS)

        self.assertEqual(again["reason_code"], "OK")
        self.assertEqual(restarted["reason_code"], "OK")
        self.assertEqual(self.state.idempotency_result("ipipe.build-binding:run-1:build-1"), stored)

    def test_rerun_after_monitor_restart_rebuilds_stage_ownership(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.builds["build-1"] = build(
            status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}]
        )
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "RUNNING"}]

        restarted = pinned_runtime(self.state, self.ledger, self.api)
        result = restarted.rerun("stage-1", approval)

        self.assertEqual(result["reason_code"], "OK")

    def test_a_failed_stage_rerun_is_refused_after_its_configuration_changes(self):
        # GPT-6.1-Sol round 4: contract 3.3 binds failed-stage re-runs to the live config.
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.api.pipeline = {**self.api.pipeline, "stages": [
            {"stageConfId": "c1", "stageName": "unit", "groupJobs": [[
                {"jobConfId": "j1", "realJob": {"shellConf": {"command": "make test"}}}]]}]}
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        hashed = self.runtime.rerun_input_hash("stage-1")
        self.assertIn("config_hash", hashed["binding"]["stage_plan"])
        approval = approved(self.ledger, "G8", hashed["input_hash"])
        self.api.pipeline["stages"][0]["groupJobs"][0][0]["realJob"]["shellConf"]["command"] = "make other"
        result = self.runtime.rerun("stage-1", approval)
        self.assertEqual(result["reason_code"], "APPROVAL_INPUT_MISMATCH", result)
        self.assertFalse(any(c[0] == "manual_execute_stage" for c in self.api.calls))

    def test_rerun_g8_hash_binds_complete_pipeline_and_environment_identity(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.builds["build-1"] = build(
            status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}]
        )
        result = self.runtime.rerun("stage-1", approval)
        self.assertEqual(result["reason_code"], "OK")

    def test_pending_rerun_restart_queries_only_and_non_owner_never_writes(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.rerun_result = TimeoutError("unknown")
        first = self.runtime.rerun("stage-1", approval)
        second = pinned_runtime(self.state, self.ledger, self.api).rerun("stage-1", approval)
        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN")
        self.assertEqual(second["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_pending_rerun_restart_queries_and_receipts_an_already_reexecuted_stage(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.rerun_result = TimeoutError("unknown")
        first = self.runtime.rerun("stage-1", approval)
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        restarted = pinned_runtime(self.state, self.ledger, self.api)
        reconciled = restarted.rerun("stage-1", approval)
        replay = restarted.rerun("stage-1", approval)
        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN")
        self.assertEqual(reconciled["reason_code"], "OK")
        self.assertEqual(replay, reconciled)
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    # --- unknown rerun outcomes keep a redacted shape; reconcile names what it saw ---

    def _observations(self, operation):
        return [
            item["observation"] for item in self.state.external_observations("run-1")
            if item["observation"].get("operation") == operation
        ]

    def test_unknown_rerun_persists_redacted_response_shape_without_secrets(self):
        self._failed_stage()
        approval = self._g8()
        self.api.rerun_result = IpipeTransportError(
            "PIPELINE_TRANSIENT", status=502, transient=True,
            diagnostic={
                "http_status": 502, "body_truncated": False,
                "body_excerpt": '{"code":502,"msg":"upstream https://irepo.example/a?sig=SIGNED failed"}',
            },
        )
        result = self.runtime.rerun("stage-1", approval)
        self.assertEqual(result["reason_code"], "RERUN_RESULT_UNKNOWN")
        (observed,) = self._observations("ipipe.rerun.write_error")
        self.assertEqual(observed["http_status"], 502)
        self.assertEqual(observed["body_keys"], ["code", "msg"])
        self.assertEqual(observed["business_code"], 502)
        text = json.dumps(observed)
        self.assertNotIn("SIGNED", text)
        self.assertNotIn("https://", text)

    def test_unknown_rerun_from_a_timeout_records_only_the_error_type(self):
        self._failed_stage()
        self.api.rerun_result = TimeoutError("slow")
        self.runtime.rerun("stage-1", self._g8())
        (observed,) = self._observations("ipipe.rerun.write_error")
        self.assertEqual(observed, {"operation": "ipipe.rerun.write_error", "error_type": "TimeoutError",
                                    "before_dispatch": False})

    def test_transport_keeps_the_shape_of_an_unparseable_success_body(self):
        def sender(method, url, headers, body, timeout, limit):
            return {"status": 200, "body": "plain text ok, token=abc"}

        transport = IpipeHttpTransport(token="secret-value", sender=sender)
        with self.assertRaises(IpipeTransportError) as caught:
            transport.request("POST", "/write", body={})
        self.assertEqual(caught.exception.reason_code, "IPIPE_RESPONSE_INVALID")
        self.assertEqual(caught.exception.diagnostic["http_status"], 200)
        self.assertEqual(caught.exception.diagnostic["response_type"], "non_json_text")
        self.assertNotIn("abc", json.dumps(caught.exception.diagnostic))

    def test_reconcile_separates_unknown_from_observed_not_started(self):
        self._failed_stage()
        approval = self._g8()
        self.api.rerun_result = TimeoutError("unknown")
        self.assertEqual(self.runtime.rerun("stage-1", approval)["reason_code"], "RERUN_RESULT_UNKNOWN")
        within_grace = self.runtime.reconcile_rerun("stage-1", approval)
        self.assertEqual(within_grace["reason_code"], "QUERY_REQUIRED")
        self.runtime.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=900)
        unproven = self.runtime.reconcile_rerun("stage-1", approval)
        self.assertEqual(unproven["reason_code"], "RERUN_NOT_STARTED_UNPROVEN")
        self.assertIs(unproven["abandon_eligible"], True)
        self.assertIs(unproven["rejection_proven"], False)
        self.assertIs(unproven["retry_allowed"], False)
        self.assertEqual(unproven["evidence"]["stage_status"], "FAIL")
        self.assertEqual(len(self._observations("ipipe.rerun.reconcile")), 1)
        self.assertEqual(len(self._posts()), 1)

    def test_abandoned_intent_replays_for_old_g8_and_unblocks_a_new_binding(self):
        self._failed_stage()
        approval = self._g8()
        self.api.rerun_result = TimeoutError("unknown")
        first = self.runtime.rerun("stage-1", approval)
        self.runtime.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=900)
        self.assertEqual(
            self.runtime.reconcile_rerun("stage-1", approval)["reason_code"], "RERUN_NOT_STARTED_UNPROVEN"
        )
        options = {"parameters": {"k": "v"}}
        blocked = self.runtime.rerun("stage-1", self._g8(**options), **options)
        self.assertEqual(blocked["reason_code"], "QUERY_REQUIRED")
        self.state.abandon_intent(first["intent_id"], "no start observed after grace", "tester")
        # The same binding maps to the same approval id, so the old G8 only replays the
        # abandonment; re-initiating needs a genuinely different approved binding.
        replay = self.runtime.rerun("stage-1", approval)
        self.assertEqual(replay["reason_code"], "INTENT_ABANDONED")
        self.api.rerun_result = {"stageBuildId": "stage-1", "status": "RUNNING"}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        started = self.runtime.rerun("stage-1", self._g8(**options), **options)
        self.assertEqual(started["reason_code"], "OK")
        self.assertEqual(len(self._posts()), 2)

    # --- a stage may be re-run again under a new G8; replays and unknowns stay safe ---

    def _failed_stage(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

    def _posts(self):
        return [call for call in self.api.calls if call[0] == "manual_execute_stage"]

    def _g8(self, **options):
        return approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1", **options)["input_hash"])

    def _finish_as_failed(self):
        self.api.builds["build-1"] = build(
            status="FAIL", stages=[{"id": "stage-1", "stageName": "unit", "status": "FAIL"}]
        )

    def test_same_stage_can_be_rerun_again_under_a_new_g8_with_new_parameters(self):
        self._failed_stage()
        first_approval = self._g8()
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        first = self.runtime.rerun("stage-1", first_approval)
        self.assertEqual(first["reason_code"], "OK")

        self._finish_as_failed()
        new_parameters = {"test_cr_id": "122402145"}
        second_approval = self._g8(parameters=new_parameters)
        self.assertNotEqual(second_approval["approval_id"], first_approval["approval_id"])
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        second = self.runtime.rerun("stage-1", second_approval, parameters=new_parameters)

        self.assertEqual(second["reason_code"], "OK", second)
        posts = self._posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2], {})
        self.assertEqual(posts[1][2], new_parameters)
        keys = {item["idempotency_key"] for item in self.state.all_intents("run-1")
                if item["operation"] == "ipipe.rerun"}
        self.assertEqual(keys, {
            f"ipipe.rerun:run-1:stage-1:{first_approval['approval_id']}",
            f"ipipe.rerun:run-1:stage-1:{second_approval['approval_id']}",
        })

    def test_replaying_either_authorisation_never_posts_again(self):
        self._failed_stage()
        first_approval = self._g8()
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        first = self.runtime.rerun("stage-1", first_approval)
        self._finish_as_failed()
        parameters = {"test_cr_id": "122402145"}
        second_approval = self._g8(parameters=parameters)
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        second = self.runtime.rerun("stage-1", second_approval, parameters=parameters)

        restarted = pinned_runtime(self.state, self.ledger, self.api)
        self.assertEqual(restarted.rerun("stage-1", second_approval, parameters=parameters), second)
        self.assertEqual(restarted.rerun("stage-1", first_approval), first)
        self.assertEqual(restarted.reconcile_rerun("stage-1", first_approval), first)
        self.assertEqual(restarted.reconcile_rerun("stage-1", second_approval), second)
        self.assertEqual(len(self._posts()), 2)

    def test_a_stage_continued_by_an_earlier_attempt_is_not_this_attempts_evidence(self):
        # A-13(b): attempt 1 continued stage-1 in place (same id) and was receipted.
        # Attempt 2's POST is lost; stage-1 still shows attempt 1's run. Its start is
        # recent enough to pass the intent-time check, but it predates attempt 1's
        # receipt, so it is not a new execution.
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        first_approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        started = int(time.time() * 1000) - 5000
        running = {"id": "stage-1", "stageName": "unit", "status": "RUNNING", "startTime": started}
        self.api.rerun_result = {"stageBuildId": "stage-1", "status": "RUNNING"}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[running])
        self.api.stages["build-1"] = [running]
        first = self.runtime.rerun("stage-1", first_approval)
        self.assertEqual(first["reason_code"], "OK", first)

        options = {"parameters": {"k": "v"}}
        second_approval = approved(
            self.ledger, "G8", self.runtime.rerun_input_hash("stage-1", **options)["input_hash"])
        self.api.rerun_result = TimeoutError("unknown")
        second = self.runtime.rerun("stage-1", second_approval, **options)
        settled = self.runtime.reconcile_rerun("stage-1", second_approval)
        self.assertEqual(second["reason_code"], "RERUN_RESULT_UNKNOWN", second)
        self.assertEqual(settled["reason_code"], "QUERY_REQUIRED", settled)

        # A later execution of the same stage id (started after attempt 1's receipt) is.
        later = {**running, "startTime": int(time.time() * 1000) + 2000}
        self.api.builds["build-1"] = build(status="RUNNING", stages=[later])
        self.api.stages["build-1"] = [later]
        self.assertEqual(self.runtime.reconcile_rerun("stage-1", second_approval)["reason_code"], "OK")

    def test_two_g8s_for_one_stage_interleaved_post_at_most_once(self):
        # A-05: attempt B runs completely while attempt A sits between its "no unknown
        # prior" check and its claim. Only one of them may reach the platform.
        self._failed_stage()
        first_approval = self._g8()
        parameters = {"test_cr_id": "122402145"}
        second_approval = self._g8(parameters=parameters)
        self.api.rerun_result = TimeoutError("unknown")
        other = pinned_runtime(self.state, self.ledger, self.api)
        inner = {}
        original = self.runtime._rerun_dispatch

        def interleave(stage_build_id, call_parameters):
            if "second" not in inner:
                inner["second"] = other.rerun("stage-1", second_approval, parameters=parameters)
            return original(stage_build_id, call_parameters)

        with patch.object(self.runtime, "_rerun_dispatch", side_effect=interleave):
            first = self.runtime.rerun("stage-1", first_approval)

        self.assertEqual(inner["second"]["reason_code"], "RERUN_RESULT_UNKNOWN", inner["second"])
        self.assertEqual(first["reason_code"], "QUERY_REQUIRED", first)
        self.assertEqual(first["intent_id"], inner["second"]["intent_id"])
        self.assertEqual(len(self._posts()), 1)

    def test_two_concurrent_g8s_for_one_stage_post_at_most_once(self):
        self._failed_stage()
        approvals = [self._g8(), self._g8(parameters={"test_cr_id": "122402145"})]
        options = [{}, {"parameters": {"test_cr_id": "122402145"}}]
        self.api.rerun_result = TimeoutError("unknown")
        barrier = threading.Barrier(2)
        results = [None, None]

        def attempt(index):
            runtime = pinned_runtime(self.state, self.ledger, self.api)
            original = runtime._rerun_dispatch

            def gate(*args):
                barrier.wait(timeout=10)
                return original(*args)

            runtime._rerun_dispatch = gate
            results[index] = runtime.rerun("stage-1", approvals[index], **options[index])

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(self._posts()), 1, results)
        self.assertEqual(sorted(r["reason_code"] for r in results),
                         ["QUERY_REQUIRED", "RERUN_RESULT_UNKNOWN"])

    def test_cli_rerun_runs_under_the_run_lease(self):
        # A-05: the CLI write path takes the same per-run lease as the worker.
        import orchestrator as controller
        from worker_driver import _WORKER_LEASE_PREFIX
        with tempfile.TemporaryDirectory() as root:
            orch = controller.Orchestrator(Path(root))
            key = f"{_WORKER_LEASE_PREFIX}run-1"
            calls = []
            held = orch.recovery.locks.acquire(key, "worker-token", 300)
            self.assertTrue(held["acquired"])
            refused = controller._under_run_lease(orch, "run-1", lambda: calls.append(1) or {"ok": True})
            orch.recovery.locks.release(key, "worker-token")
            allowed = controller._under_run_lease(orch, "run-1", lambda: calls.append(2) or {"ok": True})

        self.assertEqual(refused["reason_code"], "WORKER_LEASE_HELD")
        self.assertEqual(allowed, {"ok": True})
        self.assertEqual(calls, [2])

    def test_an_unreceipted_earlier_attempt_blocks_a_new_rerun_with_query_required(self):
        self._failed_stage()
        first_approval = self._g8()
        self.api.rerun_result = TimeoutError("unknown")
        first = self.runtime.rerun("stage-1", first_approval)
        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN")

        parameters = {"test_cr_id": "122402145"}
        second_approval = self._g8(parameters=parameters)
        blocked = self.runtime.rerun("stage-1", second_approval, parameters=parameters)

        self.assertEqual(blocked["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(blocked["intent_id"], first["intent_id"])
        self.assertEqual(len(self._posts()), 1)
        self.assertEqual(
            self.runtime.reconcile_rerun("stage-1", second_approval)["reason_code"], "RERUN_CONFLICT"
        )
        # The unknown attempt can still be settled with GET only, then a new run is allowed.
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        self.assertTrue(self.runtime.reconcile_rerun("stage-1", first_approval)["ok"])
        self._finish_as_failed()
        self.api.rerun_result = {"stageBuildId": "stage-1", "status": "RUNNING"}
        self.api.on_rerun = lambda: self.api.builds["build-1"].update(
            stageBuilds=[{"id": "stage-1", "stageName": "unit", "status": "RUNNING"}])
        again = self.runtime.rerun("stage-1", second_approval, parameters=parameters)
        self.assertEqual(again["reason_code"], "OK", again)
        self.assertEqual(len(self._posts()), 2)

    def test_legacy_keyed_history_is_a_prior_attempt_and_stays_reconcilable(self):
        self._failed_stage()
        legacy_approval = self._g8()
        binding = self.runtime.rerun_input_hash("stage-1")["binding"]
        payload = {**binding, "input_hash": legacy_approval["input_hash"],
                   "approval_id": legacy_approval["approval_id"]}
        legacy_key = "ipipe.rerun:run-1:stage-1"
        claim = self.state.claim_intent("run-1", "ipipe.rerun", legacy_key, payload)
        self.assertEqual(claim["status"], "CLAIMED")

        # Pending legacy claim: reconcile-only works under its own approval (GET only),
        # and a new authorisation is refused until it is settled.
        parameters = {"test_cr_id": "122402145"}
        new_approval = self._g8(parameters=parameters)
        self.assertEqual(
            self.runtime.rerun("stage-1", new_approval, parameters=parameters)["reason_code"],
            "QUERY_REQUIRED",
        )
        self.assertEqual(
            self.runtime.reconcile_rerun("stage-1", legacy_approval)["reason_code"], "QUERY_REQUIRED"
        )
        self.assertEqual(self._posts(), [])

        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        settled = self.runtime.reconcile_rerun("stage-1", legacy_approval)
        self.assertTrue(settled["ok"], settled)
        self.assertEqual(self.runtime.rerun("stage-1", legacy_approval), settled)
        self.assertEqual(self._posts(), [])

        # The history is preserved and a fresh authorisation runs under its own key.
        self._finish_as_failed()
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        result = self.runtime.rerun("stage-1", new_approval, parameters=parameters)
        self.assertEqual(result["reason_code"], "OK", result)
        self.assertEqual(len(self._posts()), 1)
        self.assertIsNotNone(self.state.result_by_idempotency_key(legacy_key))
        self.assertEqual(self.runtime.rerun("stage-1", legacy_approval), settled)

    def test_wrong_hash_or_foreign_run_approval_is_refused_after_a_completed_rerun(self):
        self._failed_stage()
        first_approval = self._g8()
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
        self.assertEqual(self.runtime.rerun("stage-1", first_approval)["reason_code"], "OK")
        self._finish_as_failed()
        parameters = {"test_cr_id": "122402145"}
        good = self.runtime.rerun_input_hash("stage-1", parameters=parameters)["input_hash"]

        wrong_hash = approved(self.ledger, "G8", "f" * 64)
        foreign = approved(self.ledger, "G8", good, run_id="run-2")
        for supplied in (
            {"approval_id": wrong_hash["approval_id"], "input_hash": good},
            wrong_hash,
            foreign,
            {"approval_id": foreign["approval_id"], "input_hash": good},
            dict(first_approval, input_hash=good),
        ):
            result = self.runtime.rerun("stage-1", supplied, parameters=parameters)
            self.assertFalse(result["ok"], supplied)
        self.assertEqual(len(self._posts()), 1)

    def test_monitor_and_release_restore_durable_build_ownership_after_restart(self):
        self.bind_build(status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        self.api.releases = [{
            "id": "release-1", "module": "baidu/team/app", "branch": "main", "pipelineBuildId": "build-1",
            "releaseRule": "manual-approval", "releaseStatus": "SUCCESS",
            "revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "test-rev"},
        }]
        restarted = pinned_runtime(self.state, self.ledger, self.api)
        monitored = restarted.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        released = restarted.verify_release("build-1", REVISIONS)
        self.assertEqual(monitored["status"], "SUCCESS")
        self.assertEqual(released["release_id"], "release-1")

    def test_verify_release_requires_exact_build_rule_branch_and_complete_revisions(self):
        self.bind_build(status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.api.releases = [{
            "id": "release-1", "module": "baidu/team/app", "branch": "main", "pipelineBuildId": "build-1",
            "releaseRule": "manual-approval", "releaseStatus": "SUCCESS",
            "revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "stale"},
        }]
        mismatch = self.runtime.verify_release("build-1", REVISIONS)
        self.api.releases[0]["revisions"]["baidu/team/app-tests"] = "test-rev"
        exact = self.runtime.verify_release("build-1", REVISIONS)
        self.assertEqual(mismatch["reason_code"], "REVISION_MISMATCH")
        self.assertEqual(exact["reason_code"], "OK")
        self.assertEqual(exact["release_id"], "release-1")

    def test_verify_release_of_build_uses_the_builds_own_revision_set(self):
        # R4-H2 (per-module independent release): a build is verified against its OWN recorded
        # binding, so an unchanged module's already-passed build releases without the caller
        # supplying the current whole-repo revision set (which a sibling's change would shift).
        self.bind_build(status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.api.releases = [{
            "id": "release-1", "module": "baidu/team/app", "branch": "main",
            "pipelineBuildId": "build-1", "releaseRule": "manual-approval",
            "releaseStatus": "SUCCESS",
            "revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "test-rev"},
        }]
        released = self.runtime.verify_release_of_build("build-1")
        self.assertEqual((released["reason_code"], released["release_id"]), ("OK", "release-1"))
        # An unbound build has no own revision set to release against.
        self.assertEqual(
            self.runtime.verify_release_of_build("build-unknown")["reason_code"],
            "BUILD_OWNERSHIP_UNVERIFIED")

    def _branch_pipeline_fixture(self, release_status="SUCC", revision="app-rev"):
        self.bind_build(status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.api.pipelines_by_module = lambda module: [
            {"id": 900, "pipelineName": "BranchPipeline"}, {"id": "pipe-1", "pipelineName": "ChangePipeline"}]
        branch_build = {"id": "branch-1", "pipelineConfId": "900", "module": "baidu/team/app",
                        "branch": "main", "status": "SUCCEEDING",
                        "trigger": {"branch": "main", "revision": "app-rev"}}
        original = self.api.builds_by_revision
        self.api.builds_by_revision = lambda module, rev, pid: (
            [branch_build] if str(pid) == "900" else original(module, rev, pid))
        self.api.builds["branch-1"] = branch_build
        self.api.stages["branch-1"] = [{"id": "rel-stage", "stageName": "发布", "status": release_status}]
        self.api.details["rel-stage"] = {"entities": {"realJobBuilds": [[{
            "id": 5, "jobType": "AGILE_RELEASE", "status": release_status,
            "realJobBuild": {"id": 77, "version": "26.10.27.1", "revision": revision, "branch": "main",
                             "releaseOutputHttpUrl": "https://irepo.example/app/releases/26.10.27.1/files"}}]]}}

    def test_release_is_read_from_the_branch_pipeline_release_job(self):
        # The gateway answers 404 for /api/agile/getReleaseInfo; the BranchPipeline's own
        # AGILE_RELEASE job carries the version, revision and product of the release.
        self._branch_pipeline_fixture()
        released = self.runtime.verify_release_of_build("build-1")
        self.assertEqual(released["reason_code"], "OK", released)
        self.assertEqual(released["release_version"], "26.10.27.1")
        self.assertIn("ipipe:branch-build/branch-1", released["evidence_refs"])

    def test_branch_release_of_another_revision_or_not_yet_released_waits(self):
        self._branch_pipeline_fixture(revision="older-rev")
        self.assertEqual(self.runtime.verify_release_of_build("build-1")["reason_code"], "RELEASE_WAITING")
        self._branch_pipeline_fixture(release_status="WAITTING")
        waiting = self.runtime.verify_release_of_build("build-1")
        self.assertEqual(waiting["reason_code"], "RELEASE_WAITING")
        self.assertEqual(waiting["branch_build_id"], "branch-1")

    def test_verify_release_requires_current_successful_build_and_nonempty_successful_stages(self):
        self.bind_build(status="SUCCESS")
        exact_release = {
            "id": "release-1", "module": "baidu/team/app", "branch": "main", "pipelineBuildId": "build-1",
            "releaseRule": "manual-approval", "releaseStatus": "SUCCESS",
            "revisions": {"baidu/team/app": "app-rev", "baidu/team/app-tests": "test-rev"},
        }
        self.api.releases = [exact_release]
        self.api.builds["build-1"] = build(status="FAIL")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "SUCCESS"}]
        failed_build = self.runtime.verify_release("build-1", REVISIONS)
        self.api.builds["build-1"] = build(status="SUCCESS")
        self.api.stages["build-1"] = [{"id": "stage-1", "status": "FAIL"}]
        failed_stage = self.runtime.verify_release("build-1", REVISIONS)
        self.api.stages["build-1"] = []
        empty_stages = self.runtime.verify_release("build-1", REVISIONS)
        self.assertEqual(failed_build["reason_code"], "PIPELINE_NOT_SUCCESSFUL")
        self.assertEqual(failed_stage["reason_code"], "PIPELINE_NOT_SUCCESSFUL")
        self.assertEqual(empty_stages["reason_code"], "STAGE_IDENTITY_MISSING")

    def test_typed_transport_failure_is_preserved_by_monitor_and_release(self):
        self.bind_build(status="RUNNING")
        self.api.build_by_id = lambda _build_id, **_key: (_ for _ in ()).throw(
            IpipeTransportError("PERMISSION_DENIED", status=403, transient=False)
        )
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        monitored = self.runtime.monitor("build-1", deadline)
        released = self.runtime.verify_release("build-1", REVISIONS)
        self.assertEqual(monitored["reason_code"], "PERMISSION_DENIED")
        self.assertEqual(released["reason_code"], "PERMISSION_DENIED")

    def test_definite_typed_trigger_and_rerun_write_failures_are_preserved_without_replay(self):
        trigger_approval = approved(self.ledger, "G7", canonical_hash(trigger_binding()))
        self.api.trigger_result = IpipeTransportError(
            "PERMISSION_DENIED", status=403, transient=False
        )
        triggered = self.runtime.trigger(PROFILE, REVISIONS, trigger_approval)

        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        rerun_approval = approved(
            self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"]))
        )
        self.api.rerun_result = IpipeTransportError(
            "PERMISSION_DENIED", status=403, transient=False
        )
        rerun = self.runtime.rerun("stage-1", rerun_approval)

        self.assertEqual(triggered["reason_code"], "PERMISSION_DENIED")
        self.assertEqual(rerun["reason_code"], "PERMISSION_DENIED")
        self.assertEqual(sum(call[0] == "trigger_by_revision" for call in self.api.calls), 1)
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

    def test_typed_confirmation_query_failures_are_preserved_after_single_write(self):
        trigger_approval = approved(self.ledger, "G7", canonical_hash(trigger_binding()))
        original_build_by_id = self.api.build_by_id
        self.api.build_by_id = lambda _build_id, **_key: (_ for _ in ()).throw(
            IpipeTransportError("OBJECT_NOT_FOUND", status=404, transient=False)
        )
        triggered = self.runtime.trigger(PROFILE, REVISIONS, trigger_approval)
        self.api.build_by_id = original_build_by_id

        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        rerun_approval = approved(
            self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"]))
        )
        self.api.build_by_id = lambda _build_id, **_key: (_ for _ in ()).throw(
            IpipeTransportError("OBJECT_NOT_FOUND", status=404, transient=False)
        )
        rerun = self.runtime.rerun("stage-1", rerun_approval)

        self.assertEqual(triggered["reason_code"], "OBJECT_NOT_FOUND")
        self.assertEqual(rerun["reason_code"], "OBJECT_NOT_FOUND")

    def test_typed_existing_build_and_failure_evidence_queries_are_preserved(self):
        self.api.candidates = [build()]
        self.api.build_by_id = lambda _build_id, **_key: (_ for _ in ()).throw(
            IpipeTransportError("OBJECT_NOT_FOUND", status=404, transient=False)
        )
        trigger_approval = approved(self.ledger, "G7", canonical_hash(trigger_binding()))
        existing = self.runtime.trigger(PROFILE, REVISIONS, trigger_approval)

        self.api.build_by_id = FakeApi.build_by_id.__get__(self.api, FakeApi)
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        self.api.failed_jobs = lambda _build_id: (_ for _ in ()).throw(
            IpipeTransportError("AUTH_REQUIRED", status=401, transient=False)
        )
        jobs = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

        self.api.failed_jobs = lambda _build_id: []
        self.api.stage_detail = lambda _stage_id: (_ for _ in ()).throw(
            IpipeTransportError("PERMISSION_DENIED", status=403, transient=False)
        )
        detail = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )

        self.assertEqual(existing["reason_code"], "OBJECT_NOT_FOUND")
        self.assertEqual(jobs["reason_code"], "AUTH_REQUIRED")
        self.assertEqual(detail["reason_code"], "PERMISSION_DENIED")

    def test_pending_rerun_preserves_typed_reconciliation_query_failure(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        )
        approval = approved(
            self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"]))
        )
        self.api.rerun_result = TimeoutError("unknown")
        first = self.runtime.rerun("stage-1", approval)
        self.api.build_by_id = lambda _build_id, **_key: (_ for _ in ()).throw(
            IpipeTransportError("PERMISSION_DENIED", status=403, transient=False)
        )
        pending = pinned_runtime(self.state, self.ledger, self.api).rerun("stage-1", approval)

        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN")
        self.assertEqual(pending["reason_code"], "PERMISSION_DENIED")


class HttpTransportTests(unittest.TestCase):
    def test_read_retries_are_bounded_but_write_is_never_retried_and_errors_are_bounded(self):
        read_calls = []

        def transient(method, url, headers, body, timeout, body_limit):
            read_calls.append((method, timeout))
            if len(read_calls) < 3:
                raise IpipeTransportError("PIPELINE_TRANSIENT", status=503, body="x" * 9000, transient=True)
            return {"status": 200, "body": {"code": 200, "entities": {"id": "pipe-1"}}}

        transport = IpipeHttpTransport(token="secret-value", sender=transient, max_read_attempts=3, body_limit=128)
        result = transport.request("GET", "/pipeline")
        self.assertEqual(result["entities"]["id"], "pipe-1")
        self.assertEqual(len(read_calls), 3)
        write_calls = []

        def failed_write(method, url, headers, body, timeout, body_limit):
            write_calls.append(method)
            raise IpipeTransportError("PIPELINE_TRANSIENT", status=503, body="token=secret-value" + "x" * 9000, transient=True)

        writer = IpipeHttpTransport(token="secret-value", sender=failed_write, max_read_attempts=3, body_limit=128)
        with self.assertRaises(IpipeTransportError) as caught:
            writer.request("POST", "/trigger", body={})
        self.assertEqual(write_calls, ["POST"])
        self.assertLessEqual(len(caught.exception.diagnostic.get("body_excerpt", "")), 128)
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertNotIn("secret-value", json.dumps(caught.exception.diagnostic))

    def test_untrusted_sender_diagnostic_and_boundary_split_token_are_redacted(self):
        def failed(method, url, headers, body, timeout, body_limit):
            raise IpipeTransportError(
                "PIPELINE_TRANSIENT", status=503,
                body="x" * 120 + "Bearer-sensitive-value", transient=True,
                diagnostic={"raw_header": "Bearer-sensitive-value"},
            )

        transport = IpipeHttpTransport(token="sensitive-value", sender=failed, max_read_attempts=1, body_limit=128)
        with self.assertRaises(IpipeTransportError) as caught:
            transport.request("GET", "/pipeline")
        self.assertNotIn("sensitive", json.dumps(caught.exception.diagnostic))

    def test_urllib_sender_bounds_success_and_error_stream_reads_before_decode(self):
        class Body:
            def __init__(self, payload, status=200):
                self.payload = payload
                self.status = status
                self.read_sizes = []

            def read(self, size):
                self.read_sizes.append(size)
                return self.payload[:size]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def close(self):
                return None

        success = Body(b"x" * 129)
        # The read bound is `response_limit`: `body_limit` only bounds the excerpt a
        # failure carries, so a listing is read up to the larger of the two.
        transport = IpipeHttpTransport(
            token="secret-value", body_limit=64, response_limit=128, max_read_attempts=1
        )
        with patch("clients.ipipe_client.urllib.request.urlopen", return_value=success):
            with self.assertRaises(IpipeTransportError) as success_error:
                transport.request("GET", "/pipeline")

        error_body = Body(b"y" * 129, status=503)
        http_error = urllib.error.HTTPError("http://ipipe", 503, "failed", {}, error_body)
        with patch("clients.ipipe_client.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(IpipeTransportError) as failure_error:
                transport.request("GET", "/pipeline")

        self.assertEqual(success.read_sizes, [129])
        self.assertEqual(error_body.read_sizes, [129])
        self.assertEqual(success_error.exception.reason_code, "IPIPE_RESPONSE_TOO_LARGE")
        self.assertEqual(failure_error.exception.reason_code, "IPIPE_RESPONSE_TOO_LARGE")


class ApiClientShapeTests(unittest.TestCase):
    def test_a_single_build_is_selected_out_of_its_own_revision_listing(self):
        class ListingTransport:
            def __init__(self):
                self.calls = []

            def request(self, method, endpoint, **options):
                self.calls.append((method, endpoint, options))
                return {"code": 200, "entities": [{"id": 4321, "module": "baidu/team/app"}]}

        transport = ListingTransport()
        client = IpipeApiClient(transport, current_user="owner@example.test")
        key = {"module": "baidu/team/app", "revision": "app-rev", "pipeline_id": "pipe-1"}

        found = client.build_by_id("4321", **key)

        self.assertEqual(found["id"], 4321)
        self.assertEqual(transport.calls[0][1], "/api/rest/v10/pipeline-build/builds/revision")
        with self.assertRaises(IpipeTransportError) as absent:
            client.build_by_id("9999", **key)
        self.assertEqual(absent.exception.reason_code, "OBJECT_NOT_FOUND")
        self.assertFalse(absent.exception.transient)

    def test_a_stage_skipped_only_in_part_still_counts_as_passing_evidence(self):
        partly_skipped = _normalize_stage({
            "stageName": "unit",
            "status": "SKIPPED",
            "jobBuildBeans": [
                {"stageBuildId": 1, "jobName": "100G unit", "status": "SKIPPED"},
                {"stageBuildId": 1, "jobName": "25G unit", "status": "SUCC"},
            ],
        })
        wholly_skipped = _normalize_stage({
            "stageName": "unit", "status": "SKIPPED",
            "jobBuildBeans": [{"stageBuildId": 1, "jobName": "100G unit", "status": "SKIPPED"}],
        })
        skipped_with_failure = _normalize_stage({
            "stageName": "unit", "status": "SKIPPED",
            "jobBuildBeans": [
                {"stageBuildId": 1, "jobName": "25G unit", "status": "SUCC"},
                {"stageBuildId": 1, "jobName": "100G unit", "status": "FAIL"},
            ],
        })

        self.assertTrue(_stage_passed(partly_skipped))
        self.assertFalse(_stage_passed(wholly_skipped))
        self.assertFalse(_stage_passed(skipped_with_failure))
        self.assertTrue(_stage_passed(_normalize_stage({"stageName": "x", "status": "SUCC"})))
        self.assertFalse(_stage_passed(_normalize_stage({"stageName": "x", "status": "RUNNING"})))

    def test_stage_parameters_reach_the_platform_while_the_approval_stays_token_free(self):
        # A manual stage's inputs are derived, not typed. The approval and the intent record
        # the decision -- which CR, which product -- while the irepo token in the download
        # command goes only to the platform call.
        token = "82f7b234-8b56-40c5-9011-5492673ed8d8"
        parameters = {
            "test_cr_id": "122402145",
            "get_bgwagent": f'wget -O output.tar.gz --header "IREPO-TOKEN:{token}" "https://irepo/x"',
        }
        captured: dict[str, Any] = {}

        from stage_parameters import redacted

        binding = {"parameters": redacted(parameters)}

        self.assertNotIn(token, json.dumps(binding, ensure_ascii=False))
        self.assertIn("<IREPO-TOKEN>", binding["parameters"]["get_bgwagent"])
        self.assertEqual(binding["parameters"]["test_cr_id"], "122402145")
        captured["sent"] = dict(parameters)
        self.assertIn(token, captured["sent"]["get_bgwagent"])

    def _log_runtime(self, log_reader):
        directory = tempfile.mkdtemp()
        return IpipeRuntime(
            state_store=StateStore(Path(directory) / "state.sqlite"),
            approval_ledger=ApprovalLedger(Path(directory) / "approvals.sqlite"),
            run_id="run-1",
            api_transport=object(),
            log_reader=log_reader,
        )

    def test_a_job_reporting_success_over_failed_cases_is_not_passing_evidence(self):
        # BGW's new-case stage runs product cases and never checks their result, so it
        # reported SUCC over a run where every case failed. The numbers only exist in the
        # log, so the log decides -- and it also says where to go looking.
        stage = _normalize_stage({
            "id": 637340540,
            "stageName": "P0新case回归",
            "status": "SUCC",
            "jobBuildBeans": [{
                "id": 1018370712, "jobName": "P0新功能调试", "status": "SUCC",
                "logs": [{"url": "https://logonline.example/abc"}],
            }],
        })
        parsed = {
            "ok": True,
            "success_ratios": [0.0, 0.0],
            "failed_cases": ["test_stun_route_NAT44_cc_tcp_017"],
            "agent_host": "bjkjy-sys-ip-base-sep6.bjkjy.baidu.com",
            "agent_ip": "10.130.21.26",
            "workspace": "/root/workspace/abc",
            "scripts": ["script/fetch_cr.sh"],
        }
        runtime = self._log_runtime(lambda url: parsed)

        found = runtime._silent_case_failures([stage])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["job_build_id"], "1018370712")
        self.assertEqual(found[0]["failed_cases"], ["test_stun_route_NAT44_cc_tcp_017"])
        self.assertEqual(found[0]["agent_ip"], "10.130.21.26")
        self.assertEqual(found[0]["scripts"], ["script/fetch_cr.sh"])

    def test_a_job_whose_log_reports_every_case_passing_stays_passing_evidence(self):
        stage = _normalize_stage({
            "id": 1, "stageName": "p0", "status": "SUCC",
            "jobBuildBeans": [{
                "id": 2, "jobName": "run cases", "status": "SUCC",
                "logs": [{"url": "https://logonline.example/ok"}],
            }],
        })
        runtime = self._log_runtime(
            lambda url: {"ok": True, "success_ratios": [100.0], "failed_cases": []}
        )

        self.assertEqual(runtime._silent_case_failures([stage]), [])

    def test_stage_identity_falls_back_to_the_stage_build_named_by_its_jobs(self):
        listed = {
            "stageConfId": 4875695,
            "stageName": "compile",
            "status": "SUCC",
            "jobBuildBeans": [{"stageBuildId": 636306878, "jobName": "compile", "status": "SUCC"}],
        }

        self.assertEqual(_normalize_stage(listed)["stage_build_id"], "636306878")
        self.assertEqual(_normalize_stage({"id": 7, "stageName": "x"})["stage_build_id"], "7")
        self.assertEqual(_normalize_stage({"stageName": "x"})["stage_build_id"], "")

    def test_stage_job_trigger_and_manual_requests_include_required_user_identity(self):
        class CaptureTransport:
            def __init__(self):
                self.calls = []

            def request(self, method, endpoint, **options):
                self.calls.append((method, endpoint, options))
                return {"code": 200, "entities": {}}

        transport = CaptureTransport()
        client = IpipeApiClient(transport, current_user="owner@example.test")

        client.pipeline_stage_info("build-1")
        client.failed_jobs("build-1")
        client.stage_detail("stage-1")
        client.trigger_by_revision("pipe-1", "revision-1", {"mode": "remote"})
        client.manual_execute_stage("stage-1", {"confirm": True})

        self.assertEqual(
            transport.calls,
            [
                (
                    "GET",
                    "/api/agile/v1/pipelineBuilds/pipelineBuildInfos",
                    {"params": {"pipelineBuildId": "build-1", "username": "owner@example.test"}},
                ),
                (
                    "GET",
                    "/api/agile/v1/pipelineBuilds/pipelineBuildInfos",
                    {"params": {"pipelineBuildId": "build-1", "username": "owner@example.test"}},
                ),
                (
                    "GET",
                    "/api/rest/v10/stage-build/stage-1/stageAndRealJobBuilds",
                    {"params": {"currentUser": "owner@example.test"}},
                ),
                (
                    "POST",
                    "/api/rest/v10/pipeline-build/pipe-1/revision",
                    {
                        "params": {
                            "token": "",
                            "user": "owner@example.test",
                            "pipelineConfId": "pipe-1",
                            "revision": "revision-1",
                        },
                        "body": {"triggerUser": "owner@example.test", "mode": "remote"},
                    },
                ),
                (
                    "POST",
                    "/api/agile/v1/stageBuilds/build",
                    {
                        "params": {"stageBuildId": "stage-1", "username": "owner@example.test"},
                        "body": {"confirm": True},
                        "headers": {"AGILE-PLAT-NAME": "", "AGILE-PLAT-TOKEN": ""},
                    },
                ),
            ],
        )


class EvidenceOutcomeTests(unittest.TestCase):
    _BINDING = {
        "pipeline_id": "pipe-1", "module": "baidu/team/app",
        "revisions": {"business": "r2", "tests": "t2"},
        "environment_fingerprint": "env-1", "release_rule": "manual-approval",
    }

    def test_success_result_maps_to_schema_valid_outcome(self):
        outcome = evidence_outcome({
            "status": "SUCCESS", "classification": "SUCCESS", "build_id": "build-1",
            "stages": [{"stage_build_id": "stage-1", "status": "SUCCESS", "jobs": []}],
            "evidence_refs": ["ipipe:build/build-1"],
        })
        self.assertEqual(outcome["status"], "SUCCESS")
        self.assertIsNone(outcome["failure_signature"])
        # A stage the API did not break into jobs contributes one implicit job.
        self.assertEqual(outcome["stages"], [{"stage_id": "stage-1", "status": "SUCCESS", "job_ids": ["stage-1-job"]}])
        self.assertEqual(outcome["jobs"], [
            {"job_id": "stage-1-job", "status": "SUCCESS", "evidence_refs": ["ipipe:job/stage-1-job"]}
        ])
        self.assertEqual(validate_named_schema({**outcome, **self._BINDING}, "ipipe-evidence"), [])

    def test_failure_result_maps_status_classification_and_jobs(self):
        outcome = evidence_outcome({
            "status": "FAILURE", "classification": "CODE_FAILURE", "failure_signature": "sig-1",
            "build_id": "build-1",
            "stages": [{"stage_build_id": "stage-1", "status": "FAIL",
                        "jobs": [{"job_build_id": "job-1", "status": "FAIL"}]}],
            "evidence_refs": ["ipipe:build/build-1", "ipipe:job/job-1"],
        })
        self.assertEqual((outcome["status"], outcome["classification"], outcome["failure_signature"]),
                         ("FAILURE", "CODE_FAILURE", "sig-1"))
        self.assertEqual(outcome["stages"][0], {"stage_id": "stage-1", "status": "FAILURE", "job_ids": ["job-1"]})
        self.assertEqual(outcome["jobs"][0],
                         {"job_id": "job-1", "status": "FAILURE", "evidence_refs": ["ipipe:job/job-1"]})
        self.assertEqual(validate_named_schema({**outcome, **self._BINDING}, "ipipe-evidence"), [])


class FailureSignatureTests(unittest.TestCase):
    """MEDIUM-004: the failure signature identifies a root cause across runs, not a build."""

    def test_signature_is_stable_across_builds(self):
        from clients.ipipe_runtime import _failure_signature

        stages_a = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL", "stage_build_id": "sb-A"}]
        stages_b = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL", "stage_build_id": "sb-B"}]
        jobs_a = [{"name": "compile", "status": "FAIL", "job_build_id": "jb-A"}]
        jobs_b = [{"name": "compile", "status": "FAIL", "job_build_id": "jb-B"}]
        # Same pipeline/module/stage/job structure in a fresh build is the same root cause.
        self.assertEqual(
            _failure_signature("pipe-1", "bgw", stages_a, jobs_a),
            _failure_signature("pipe-1", "bgw", stages_b, jobs_b),
        )

    def test_a_different_failed_stage_is_a_different_root_cause(self):
        from clients.ipipe_runtime import _failure_signature

        base = _failure_signature(
            "pipe-1", "bgw", [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}], []
        )
        other = _failure_signature(
            "pipe-1", "bgw", [{"stage_conf_id": "conf-2", "name": "lint", "status": "FAIL"}], []
        )
        self.assertNotEqual(base, other)

    def test_a_nameless_job_contributes_no_occurrence_noise(self):
        from clients.ipipe_runtime import _failure_signature

        stages = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}]
        # A normalized job with no name falls back to its build id — it must not enter the
        # signature, or the same failure would look new every build.
        nameless_a = _failure_signature(
            "pipe-1", "bgw", stages, [{"name": "jb-A", "job_build_id": "jb-A", "status": "FAIL"}]
        )
        nameless_b = _failure_signature(
            "pipe-1", "bgw", stages, [{"name": "jb-B", "job_build_id": "jb-B", "status": "FAIL"}]
        )
        self.assertEqual(nameless_a, nameless_b)
        self.assertEqual(nameless_a, _failure_signature("pipe-1", "bgw", stages, []))

    def test_distinct_errors_at_the_same_job_are_not_merged(self):
        # R-M4: two different failures at the same stage/job (different assertion messages)
        # must produce DIFFERENT signatures, or unrelated bugs collapse into one cross-run
        # FailureCase and get wrongly escalated.
        from clients.ipipe_runtime import _failure_signature

        stages = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}]
        one = _failure_signature("pipe-1", "bgw", stages, [
            {"name": "run-tests", "status": "FAIL", "message": "AssertionError: expected user.id"}])
        two = _failure_signature("pipe-1", "bgw", stages, [
            {"name": "run-tests", "status": "FAIL", "message": "TimeoutError: connect to db"}])
        self.assertNotEqual(one, two)

    def test_same_error_matches_across_builds_despite_volatile_tokens(self):
        # R-M4 / R4-M3: the same root cause whose message differs only by path (with its line),
        # a hex build/commit id, a UUID, and a timestamp still maps to one signature.
        from clients.ipipe_runtime import _failure_signature

        stages = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}]
        first = _failure_signature("pipe-1", "bgw", stages, [{
            "name": "run-tests", "status": "FAIL", "job_build_id": "jb-A",
            "message": ("AssertionError at /work/b-1234/test_user.py:42 rev 6f3a9c1d "
                        "req 12345678-abcd-4eef-8afe-123456789abc at 2026-09-21T10:00:00Z"),
        }])
        second = _failure_signature("pipe-1", "bgw", stages, [{
            "name": "run-tests", "status": "FAIL", "job_build_id": "jb-B",
            "message": ("AssertionError at /work/x-9/test_user.py:57 rev 8b2e5f7a "
                        "req 87654321-dead-4ace-9abe-cba987654321 at 2026-09-22T11:30:00Z"),
        }])
        self.assertEqual(first, second)

    def test_distinct_error_codes_are_not_merged(self):
        # R4-M3: a plain decimal like an HTTP status is part of the root cause — 401 and 503
        # must NOT collapse to one signature (blanket digit removal would merge them).
        from clients.ipipe_runtime import _failure_signature

        stages = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}]
        unauthorized = _failure_signature("pipe-1", "bgw", stages, [
            {"name": "call", "status": "FAIL", "message": "request failed with status 401"}])
        unavailable = _failure_signature("pipe-1", "bgw", stages, [
            {"name": "call", "status": "FAIL", "message": "request failed with status 503"}])
        self.assertNotEqual(unauthorized, unavailable)

    def test_uuid_only_change_keeps_the_same_signature(self):
        # R4-M3: the same error differing only by a UUID matches — UUIDs are normalized as a
        # unit, so their inner 4-char groups do not survive to split the signature.
        from clients.ipipe_runtime import _failure_signature

        stages = [{"stage_conf_id": "conf-1", "name": "unit", "status": "FAIL"}]
        one = _failure_signature("pipe-1", "bgw", stages, [{
            "name": "call", "status": "FAIL",
            "message": "request 12345678-abcd-4eef-8afe-123456789abc rejected"}])
        two = _failure_signature("pipe-1", "bgw", stages, [{
            "name": "call", "status": "FAIL",
            "message": "request 87654321-dead-4ace-9abe-cba987654321 rejected"}])
        self.assertEqual(one, two)


class _DeclaringApi(FakeApi):
    """FakeApi that also serves the live pipeline configuration and a stage declaration."""

    def __init__(self, configuration_stages, stage_conf_id=4875670):
        super().__init__()
        self.stage_conf_id = stage_conf_id
        self.configuration_stages = configuration_stages
        self.capability = {"supported": True, "evidence_ref": "fake", "contract_hash": "hash"}

    def get_pipeline_by_id(self, pipeline_id, brief=True):
        self.calls.append(("get_pipeline_by_id", pipeline_id))
        return {**self.pipeline, "stages": self.configuration_stages}

    def stage_job_override_capability(self):
        return dict(self.capability)

    def stage_detail(self, stage_id):
        self.calls.append(("stage_detail", stage_id))
        return {"entities": {
            "stageBuild": {
                "stageBuildId": stage_id, "pipelineBuildId": "build-1", "pipelineConfId": "pipe-1",
                "stageConfId": self.stage_conf_id, "stageName": "unit",
                "manualParams": None, "hasDynamicParams": False,
            },
            "realJobBuilds": [[{"jobConfId": 3}]],
        }}


def _own_stage_configuration(conf_id=4875670, up_stage_param=False):
    return [{
        "stageConfId": conf_id, "stageName": "unit",
        "paramBeans": [
            {"name": "get_bgw_test_case", "upStageParam": up_stage_param},
            {"name": "get_bgwagent", "upStageParam": up_stage_param},
        ],
        "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
            "command": 'echo "${get_bgw_test_case} ${get_bgwagent}"'}}}]],
    }]


def _upstream_configuration(conf_id=4875670):
    return [
        {"stageConfId": 1, "paramBeans": [{"name": "get_bgwagent"}]},
        {"stageConfId": conf_id, "stageName": "unit", "upstreamStageId": 1,
         "paramBeans": [{"name": "get_bgw_test_case"}],
         "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
             "command": 'echo "${get_bgw_test_case} ${get_bgwagent}"'}}}]]},
    ]


STAGE_PARAMETERS = {"get_bgw_test_case": "case", "get_bgwagent": "agent"}


class StageOwnParameterRerunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def arrange(self, configuration_stages):
        self.api = _DeclaringApi(configuration_stages)
        self.runtime = pinned_runtime(self.state, self.ledger, self.api, sleeper=lambda _seconds: None)
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL", "stageConfId": 4875670}
        item = build(status="FAIL", stages=[failed])
        self.api.candidates = [item]
        self.api.builds["build-1"] = item
        self.assertEqual(self.runtime.discover(PROFILE, REVISIONS)["reason_code"], "OK")
        self.api.stages["build-1"] = [failed]
        self.runtime.monitor(
            "build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

    def approval(self):
        bound = self.runtime.rerun_input_hash("stage-1", parameters=STAGE_PARAMETERS)
        self.assertEqual(bound["reason_code"], "OK", bound)
        return approved(self.ledger, "G8", bound["input_hash"])

    def posts(self):
        return [call for call in self.api.calls if call[0] == "manual_execute_stage"]

    def rerun_intents(self):
        return [i for i in self.state.all_intents("run-1") if i["operation"] == "ipipe.rerun"]

    def running(self):
        self.api.builds["build-1"] = build(
            status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])

    def test_declaration_on_the_stage_itself_uses_manual_execute_stage_once(self):
        self.arrange(_own_stage_configuration())
        declaration = self.runtime.stage_parameter_names("stage-1")
        self.assertTrue(declaration["requires_job_override"])
        self.assertEqual(
            {entry["scope"] for entries in declaration["declarations"].values() for entry in entries},
            {"stage:4875670"})
        approval = self.approval()
        self.running()
        result = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(result["reason_code"], "OK", result)
        self.assertEqual(self.posts(), [("manual_execute_stage", "stage-1", STAGE_PARAMETERS)])
        self.assertIsInstance(self.posts()[0][2], dict)

    def test_inherited_or_upstream_flagged_declaration_stays_unsupported_without_post(self):
        for label, configuration in (
            ("upstream stage", _upstream_configuration()),
            ("upStageParam", _own_stage_configuration(up_stage_param=True)),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                self.state = StateStore(Path(directory) / "state.sqlite")
                self.ledger = ApprovalLedger(Path(directory) / "approvals.sqlite")
                self.arrange(configuration)
                hashed = self.runtime.rerun_input_hash("stage-1", parameters=STAGE_PARAMETERS)
                self.assertEqual(hashed["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")
                result = self.runtime.rerun(
                    "stage-1", {"approval_id": "any", "input_hash": "any"},
                    parameters=dict(STAGE_PARAMETERS))
                self.assertEqual(result["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")
                self.assertEqual(self.posts(), [])
                self.assertEqual(self.rerun_intents(), [])

    def test_job_scope_names_fail_closed(self):
        from stage_parameters import job_scope_names
        own = {"names": ["a"], "declarations": {"a": [
            {"source": "stage:7.paramBeans", "scope": "stage:7", "upStageParam": False},
            {"source": "stage:7.inParamsMap", "scope": "stage:7"}]}}
        self.assertEqual(job_scope_names(own, 7), [])
        self.assertEqual(job_scope_names(own, 8), ["a"])
        nested = {"names": ["a"], "declarations": {"a": [
            {"source": "x.paramBeans", "scope": "stage:7.groupJobs[0][0]"}]}}
        self.assertEqual(job_scope_names(nested, 7), ["a"])
        self.assertEqual(job_scope_names({"names": ["a"]}, 7), ["a"])
        self.assertEqual(job_scope_names({"names": ["a"], "declarations": {}}, 7), ["a"])

    def test_missing_dispatch_method_is_a_local_error_that_claims_nothing(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.running()
        original = self.api.manual_execute_stage
        self.api.manual_execute_stage = None
        result = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(result["reason_code"], "RERUN_LOCAL_ERROR")
        self.assertIs(result["retry_allowed"], False)
        self.assertEqual(self.rerun_intents(), [])
        self.assertEqual(self.state.pending_intents("run-1"), [])
        # Nothing was claimed, so the same approval is still usable once the fault is gone.
        self.api.manual_execute_stage = original
        again = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(again["reason_code"], "OK", again)

    def test_wrong_arity_dispatch_is_a_local_error_that_claims_nothing(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.manual_execute_stage = lambda only_one: None
        result = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(result["reason_code"], "RERUN_LOCAL_ERROR")
        self.assertEqual(self.rerun_intents(), [])

    def test_call_site_error_after_claim_is_closed_not_left_unknown(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.manual_execute_stage = lambda only_one: None
        # Defeat the pre-claim signature check to exercise the post-claim safeguard.
        with patch("clients.ipipe_runtime.inspect.signature", side_effect=ValueError):
            result = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(result["reason_code"], "RERUN_LOCAL_ERROR")
        self.assertEqual(self.state.pending_intents("run-1"), [])
        (intent,) = self.rerun_intents()
        receipt = self.state.result_by_idempotency_key(intent["idempotency_key"])["receipt"]["response"]
        self.assertEqual(receipt["reason_code"], "INTENT_ABANDONED")
        (observed,) = [
            o["observation"] for o in self.state.external_observations("run-1")
            if o["observation"].get("operation") == "ipipe.rerun.write_error"]
        self.assertEqual(observed["error_type"], "TypeError")

    def _local_error_attempt(self, approval):
        self.api.manual_execute_stage = lambda only_one: None
        with patch("clients.ipipe_runtime.inspect.signature", side_effect=ValueError):
            return self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))

    def test_same_approval_may_be_reissued_once_after_a_proven_local_error(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        first = self._local_error_attempt(approval)
        self.assertEqual(first["reason_code"], "RERUN_LOCAL_ERROR")
        self.assertEqual(self.posts(), [])
        del self.api.manual_execute_stage  # back to the class implementation
        self.running()
        again = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(again["reason_code"], "OK", again)
        old, new = self.rerun_intents()
        self.assertEqual(new["idempotency_key"], old["idempotency_key"] + ":retry-1")
        self.assertEqual(len(self.posts()), 1)

    def test_second_local_error_is_final_for_that_approval(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self._local_error_attempt(approval)
        self.assertEqual(self._local_error_attempt(approval)["reason_code"], "RERUN_LOCAL_ERROR")
        del self.api.manual_execute_stage
        third = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(third["reason_code"], "INTENT_ABANDONED")
        self.assertEqual(len(self.rerun_intents()), 2)
        self.assertEqual(self.posts(), [])

    def test_manual_abandon_without_the_marker_stays_final(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.rerun_result = AttributeError("raised inside the adapter")
        self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        (intent,) = self.rerun_intents()
        self.state.abandon_intent(intent["intent_id"], "gave up", "owner")
        again = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(again["reason_code"], "INTENT_ABANDONED")
        self.assertEqual(len(self.rerun_intents()), 1)

    def test_an_error_raised_inside_the_adapter_cannot_be_abandoned_as_local(self):
        # A-02: an AttributeError/TypeError raised deep in the client (here: inside
        # manual_execute_stage, after the request may already have been sent) is not
        # proof that nothing left. `--local-error-proven` is refused and the approval
        # cannot be re-sent; only a plain abandonment (final) is possible.
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.rerun_result = AttributeError("raised inside the adapter")
        first = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        (intent,) = self.rerun_intents()
        (observed,) = [
            o["observation"] for o in self.state.external_observations("run-1")
            if o["observation"].get("operation") == "ipipe.rerun.write_error"]

        with self.assertRaisesRegex(ValueError, "LOCAL_ERROR_NOT_PROVEN"):
            self.state.abandon_intent(intent["intent_id"], "local bug", "owner", local_error_proven=True)
        self.api.rerun_result = {"stageBuildId": "stage-1", "status": "RUNNING"}
        again = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))

        self.assertEqual(first["reason_code"], "RERUN_RESULT_UNKNOWN", first)
        self.assertIs(observed["before_dispatch"], False)
        self.assertEqual(again["reason_code"], "QUERY_REQUIRED", again)
        self.assertEqual(len(self.posts()), 1)

    def test_a_call_site_error_is_proven_local_and_allows_exactly_one_reissue(self):
        # A-02 counterpart: the call itself raised (wrong arity), so no request exists;
        # the runtime records before_dispatch=true, closes the claim as proven local,
        # and the same approval may be sent once.
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        first = self._local_error_attempt(approval)
        (observed,) = [
            o["observation"] for o in self.state.external_observations("run-1")
            if o["observation"].get("operation") == "ipipe.rerun.write_error"]
        del self.api.manual_execute_stage
        self.running()
        again = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))

        self.assertEqual(first["reason_code"], "RERUN_LOCAL_ERROR")
        self.assertIs(observed["before_dispatch"], True)
        self.assertEqual(again["reason_code"], "OK", again)
        self.assertEqual(len(self.posts()), 1)

    def test_a_legacy_proven_abandonment_still_replays_its_completed_retry(self):
        # A receipt written before `before_dispatch` existed (the real d63d8af4 case)
        # keeps resolving to the retry already made under it; it grants no new one.
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self._local_error_attempt(approval)
        del self.api.manual_execute_stage
        self.running()
        done = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(done["reason_code"], "OK", done)
        with sqlite3.connect(self.state.database_path) as connection:
            for (row_id, text) in connection.execute(
                "SELECT rowid, observation_json FROM external_observations").fetchall():
                value = json.loads(text)
                value.pop("before_dispatch", None)
                connection.execute(
                    "UPDATE external_observations SET observation_json = ? WHERE rowid = ?",
                    (json.dumps(value), row_id))

        replay = pinned_runtime(self.state, self.ledger, self.api).rerun(
            "stage-1", approval, parameters=dict(STAGE_PARAMETERS))

        self.assertEqual(replay, done)
        self.assertEqual(len(self.posts()), 1)

    def test_marker_without_a_recorded_observation_is_not_trusted(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.rerun_result = TimeoutError("unknown")
        self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        (intent,) = self.rerun_intents()
        with self.assertRaisesRegex(ValueError, "LOCAL_ERROR_NOT_PROVEN"):
            self.state.abandon_intent(intent["intent_id"], "x", "owner", local_error_proven=True)

    def test_error_inside_the_client_after_dispatch_stays_unknown(self):
        self.arrange(_own_stage_configuration())
        approval = self.approval()
        self.api.rerun_result = AttributeError("raised inside the adapter")
        result = self.runtime.rerun("stage-1", approval, parameters=dict(STAGE_PARAMETERS))
        self.assertEqual(result["reason_code"], "RERUN_RESULT_UNKNOWN")
        self.assertEqual(len(self.state.pending_intents("run-1")), 1)

    def test_every_api_method_the_runtime_calls_exists_on_the_real_client(self):
        import clients.ipipe_runtime as runtime_module
        import product_policy

        client_methods = {
            name for name in dir(IpipeApiClient)
            if not name.startswith("_") and callable(getattr(IpipeApiClient, name))}
        # Reachable only after `compile_only_capability()` reports `supported`, which the
        # real adapter never does (asserted below), so `compile_binding` raises first.
        gated_by_compile_capability = {"reconcile_branch_compile", "trigger_branch_compile"}
        for module in (runtime_module, product_policy):
            referenced = set(re.findall(
                r"\bapi\.([A-Za-z_]\w*)", Path(module.__file__).read_text(encoding="utf-8")))
            self.assertGreater(len(referenced), 5, module.__name__)
            missing = referenced - client_methods - gated_by_compile_capability
            self.assertEqual(missing, set(), f"{module.__name__} calls API methods that do not exist")
        self.assertFalse(IpipeApiClient(object(), current_user="u").compile_only_capability()["supported"])
        self.assertNotIn("execute_stage_job_override",
                         Path(runtime_module.__file__).read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
