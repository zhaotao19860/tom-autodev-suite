import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport, IpipeTransportError
from clients.ipipe_runtime import IpipeRuntime, _normalize_stage, _stage_passed
from project_registry import validate_profile
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

    def get_pipeline_by_id(self, pipeline_id):
        self.calls.append(("get_pipeline_by_id", pipeline_id))
        return self.pipeline

    def builds_by_revision(self, module, revision, pipeline_id):
        self.calls.append(("builds_by_revision", module, revision, pipeline_id))
        return list(self.candidates)

    def build_by_id(self, build_id, *, module=None, revision=None, pipeline_id=None):
        self.calls.append(("build_by_id", build_id))
        return dict(self.builds[build_id])

    def pipeline_stage_info(self, build_id):
        self.calls.append(("pipeline_stage_info", build_id))
        return [dict(stage) for stage in self.stages.get(build_id, [])]

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
        self.runtime = pinned_runtime(self.state, self.ledger, self.api)

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

    def test_trigger_requires_exact_run_bound_g8_hash(self):
        input_hash = canonical_hash(trigger_binding())
        wrong_gate = approved(self.ledger, "G7", input_hash)
        wrong_hash = approved(self.ledger, "G8", "0" * 64)
        first = self.runtime.trigger(PROFILE, REVISIONS, wrong_gate)
        second = self.runtime.trigger(PROFILE, REVISIONS, wrong_hash)
        self.assertEqual(first["reason_code"], "APPROVAL_GATE_MISMATCH")
        self.assertEqual(second["reason_code"], "APPROVAL_INPUT_MISMATCH")
        self.assertFalse(any(call[0] == "trigger_by_revision" for call in self.api.calls))

    def test_trigger_verifies_response_and_unknown_result_is_never_replayed(self):
        approval = approved(self.ledger, "G8", canonical_hash(trigger_binding()))
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
                approval = approved(ledger, "G8", canonical_hash(trigger_binding()))

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

    def test_typed_transport_failure_is_preserved_by_discovery(self):
        def denied(_pipeline_id):
            raise IpipeTransportError("AUTH_REQUIRED", status=401, transient=False)

        self.api.get_pipeline_by_id = denied
        result = self.runtime.discover(PROFILE, REVISIONS)
        self.assertEqual(result["reason_code"], "AUTH_REQUIRED")

    def test_concurrent_trigger_allows_one_writer_then_replays_receipt(self):
        approval = approved(self.ledger, "G8", canonical_hash(trigger_binding()))
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

    def test_rerun_requires_owned_failed_stage_exact_g8_and_has_durable_budget(self):
        failed = {"id": "stage-1", "stageName": "unit", "status": "FAIL"}
        self.bind_build(status="FAIL", stages=[failed])
        self.api.stages["build-1"] = [failed]
        monitored = self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        approval = approved(self.ledger, "G8", canonical_hash(rerun_binding(monitored["failure_signature"])))
        self.api.builds["build-1"] = build(status="RUNNING", stages=[{"id": "stage-1", "status": "RUNNING"}])
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
        self.assertEqual(forged["reason_code"], "RERUN_CONFLICT")
        self.assertEqual(sum(call[0] == "manual_execute_stage" for call in self.api.calls), 1)

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
        trigger_approval = approved(self.ledger, "G8", canonical_hash(trigger_binding()))
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
        trigger_approval = approved(self.ledger, "G8", canonical_hash(trigger_binding()))
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
        trigger_approval = approved(self.ledger, "G8", canonical_hash(trigger_binding()))
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


if __name__ == "__main__":
    unittest.main()
