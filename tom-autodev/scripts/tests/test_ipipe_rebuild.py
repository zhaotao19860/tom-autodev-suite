"""`ipipe-rebuild`: a controlled second build of the same revisions when the product is gone."""
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ipipe_rebuild
import orchestrator as controller
import workflow_spec
from approval_summary import pinned_context
from clients.ipipe_client import IpipeTransportError
from pipeline_plan import frozen_plan
from test_ipipe_runtime import FakeApi, approved
import test_pipeline_plan
from transition_policy import TransitionPolicy
import worker_driver

RUN_MODULE = "A"
GONE = {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_status": 404}


class RebuildApi(FakeApi):
    """FakeApi plus the calls a re-build makes: trigger with an event type, download probe."""

    def __init__(self):
        super().__init__()
        self.event_types = []
        self.downloads = {}

    def trigger_by_revision(self, pipeline_id, revision, parameters, *, event_type=None):
        self.calls.append(("trigger_by_revision", pipeline_id, revision, dict(parameters)))
        self.event_types.append(event_type)
        if self.on_trigger:
            self.on_trigger()
        if isinstance(self.trigger_result, Exception):
            raise self.trigger_result
        return dict(self.trigger_result)

    def verify_product_download(self, url, module):
        return dict(self.downloads.get(url.rsplit("/", 1)[-1].split(".")[0], GONE))

    @property
    def trigger_count(self):
        return sum(1 for call in self.calls if call[0] == "trigger_by_revision")


class RebuildTestCase(unittest.TestCase):
    """A run whose only changed module A failed in IPIPE and now sits in DIAGNOSE."""

    def setUp(self):
        self.base = test_pipeline_plan.PipelinePlanTests(
            "test_plan_is_frozen_and_does_not_follow_live_cr_patchsets")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.orch, self.run = self.base.orch, self.base.run
        self.api = self.base.api = RebuildApi()
        self.base._start_unchanged()
        self.plan = frozen_plan(self.orch.state.events(self.run))
        self.target = self.plan["modules"][RUN_MODULE]
        self.api.pipeline = {"id": self.target["pipeline_id"], "module": RUN_MODULE}
        self.clock = datetime.now(timezone.utc)
        self.patches = [
            patch.object(controller, "_current_patchset", side_effect=self.current_patchset),
            patch.object(controller, "_open_test_repo_cr", return_value=None),
        ]
        for started in self.patches:
            started.start()
            self.addCleanup(started.stop)
        self.live = {"42": "a1"}
        self.fail_original()

    def current_patchset(self, module, path, change_number):
        return self.live.get(str(change_number))

    # -- fixtures -------------------------------------------------------------------

    def record(self, build_id, status, *, ci="CHANGE", minutes=0):
        return {
            "id": build_id, "pipelineConfId": self.target["pipeline_id"], "module": RUN_MODULE,
            "revision": self.target["expected_revisions"][RUN_MODULE],
            "revisions": {r["module"]: r["revision"] for r in self.plan["revision_set"]["repositories"]},
            "params": {}, "status": status, "trigger": {"ciType": ci},
            "startTime": int((self.clock + timedelta(minutes=minutes)).timestamp() * 1000),
        }

    def add_build(self, build_id, status="RUNNING", **options):
        record = self.record(build_id, status, **options)
        self.api.builds[build_id] = record
        self.api.candidates = [*self.api.candidates, record]
        compile_stage = f"{build_id}-compile"
        self.api.stages[build_id] = [
            {"id": compile_stage, "stageName": "compile", "class": "unit", "status": "SUCC"},
            {"id": f"{build_id}-unit", "stageName": "unit", "class": "unit",
             "status": "FAILED" if status in {"FAIL", "FAILED"} else "RUNNING"},
        ]
        self.api.details[compile_stage] = {"entities": {"realJobBuilds": [[{
            "id": f"{build_id}-job", "jobType": "AGILE_COMPILE", "status": "SUCC",
            "realJobBuild": {"productHttpUrl": f"https://irepo.baidu-int.com/rest/prod/v3/A/{build_id}.tar.gz"},
        }]]}}
        return record

    def fail_original(self):
        self.add_build("100", "FAILED", minutes=-60)
        result = self.base._build(RUN_MODULE, "100", "FAILURE")
        # `_build` rewrites the record without the trigger block the platform returns.
        self.api.builds["100"]["trigger"] = {"ciType": "CHANGE"}
        self.api.builds["100"]["startTime"] = self.record("100", "FAILED", minutes=-60)["startTime"]
        self.assertIn(result["reason_code"], {"OK", "PIPELINE_EVIDENCE_INCOMPLETE"}, result)
        self.assertEqual(self.orch.status(self.run)["state"], "DIAGNOSE")
        self.orch.state.record_producer_job(
            self.run, "producer:diagnose", {"phase": "DIAGNOSE", "action_id": "diag-action"})

    def runtime(self):
        return self.base._runtime()

    def new_build_on_trigger(self, build_id="101", **options):
        def start():
            self.add_build(build_id, **options)
        self.api.on_trigger = start
        self.api.trigger_result = {"id": build_id}

    def request(self, **options):
        result = ipipe_rebuild.prepare_g8(
            self.orch, self.runtime(), self.run, reason="产物 404，无法重跑任何阶段", **options)
        self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED", result)
        return result["approval_input_hash"]

    def approve(self, input_hash):
        return approved(self.orch.approvals, "G8", input_hash, self.run)

    def execute(self, input_hash=None, **options):
        input_hash = input_hash or self.request()
        grant = self.approve(input_hash)
        return ipipe_rebuild.execute(
            self.orch, self.runtime(), self.run, grant["approval_id"], input_hash, **options)

    def plan_result(self, **options):
        return ipipe_rebuild.plan(self.orch, self.runtime(), self.run,
                                  reason="产物 404", **options)

    def state(self):
        return self.orch.status(self.run)["state"]


class PreconditionTests(RebuildTestCase):
    def test_plan_binds_run_pipeline_revisions_original_build_attempt_and_reason(self):
        planned = self.plan_result()

        self.assertTrue(planned["ok"], planned)
        binding = planned["binding"]
        self.assertEqual(binding["operation"], "ipipe.rebuild")
        self.assertEqual(binding["run_id"], self.run)
        self.assertEqual(binding["pipeline_id"], self.target["pipeline_id"])
        self.assertEqual(binding["revision_set_id"], self.plan["revision_set"]["revision_set_id"])
        self.assertEqual(binding["original_build_id"], "100")
        self.assertEqual(binding["attempt"], 2)
        self.assertEqual(binding["event_type"], "GERRIT_CHANGE")
        self.assertEqual(planned["probe"]["http_status"], 404)
        self.assertEqual(planned["revisions"]["unverified"], ["tests"])
        self.assertEqual(self.api.trigger_count, 0)
        self.assertEqual(self.plan_result()["input_hash"], planned["input_hash"])
        self.assertNotEqual(
            ipipe_rebuild.plan(self.orch, self.runtime(), self.run, reason="另一个原因")["input_hash"],
            planned["input_hash"])

    def test_reason_is_mandatory(self):
        planned = ipipe_rebuild.plan(self.orch, self.runtime(), self.run, reason="  ")

        self.assertEqual(planned["reason_code"], "REBUILD_REASON_REQUIRED")

    def test_only_a_failed_pipeline_diagnosis_or_ipipe_qualifies(self):
        review = self.orch.state.transition(self.run, "DIAGNOSE", {"previous_state": "REVIEW"})
        planned = self.plan_result()
        self.assertEqual(planned["reason_code"], "REBUILD_NOT_JUSTIFIED", planned)
        self.assertEqual(planned["detail"], "DIAGNOSE_NOT_FROM_IPIPE")
        self.assertIsNotNone(review)

        self.orch.state.transition(self.run, "STOPPED", {})
        self.assertEqual(self.plan_result()["detail"], "STATE_NOT_ELIGIBLE")

    def test_product_that_still_downloads_is_not_a_reason(self):
        self.api.downloads["100"] = {"ok": True, "reason_code": "OK"}

        planned = self.plan_result()

        self.assertEqual(planned["reason_code"], "REBUILD_NOT_JUSTIFIED", planned)
        self.assertEqual(planned["detail"], "PRODUCT_STILL_DOWNLOADABLE")
        self.assertEqual(self.request_refusal()["reason_code"], "REBUILD_NOT_JUSTIFIED")

    def request_refusal(self):
        return ipipe_rebuild.prepare_g8(self.orch, self.runtime(), self.run, reason="r")

    def test_only_403_and_404_count_as_gone(self):
        for answer in (
            {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_status": 500},
            {"ok": False, "reason_code": "IREPO_TOKEN_REQUIRED"},
            {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "error_type": "TimeoutError"},
        ):
            with self.subTest(answer=answer):
                self.api.downloads["100"] = answer
                planned = self.plan_result()
                self.assertEqual(planned["reason_code"], "REBUILD_NOT_JUSTIFIED", planned)
                self.assertEqual(planned["detail"], "PRODUCT_STATE_INCONCLUSIVE")
        self.api.downloads["100"] = {"ok": False, "http_status": 403,
                                        "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED"}
        self.assertTrue(self.plan_result()["ok"])

    def test_a_moved_cr_patchset_refuses(self):
        self.live["42"] = "a2"

        planned = self.plan_result()

        self.assertEqual(planned["reason_code"], "REVISION_DRIFT", planned)
        self.assertEqual((planned["frozen"], planned["current"]), ("a1", "a2"))

    def test_an_unreadable_or_closed_cr_refuses(self):
        self.live.clear()

        self.assertEqual(self.plan_result()["reason_code"], "REVISION_UNVERIFIED")

    def test_two_owned_current_builds_are_ambiguous(self):
        self.orch.state.save_idempotency_result(
            f"ipipe.build-binding:{self.run}:other-9",
            {"run_id": self.run, "build_id": "other-9",
             "binding": {**self.orch.state.idempotency_result(
                 f"ipipe.build-binding:{self.run}:100")["binding"]}})

        planned = self.plan_result()

        self.assertEqual(planned["reason_code"], "BUILD_AMBIGUOUS", planned)


class ApprovalTests(RebuildTestCase):
    def test_request_pins_readable_material_and_executes_nothing(self):
        input_hash = self.request()

        self.assertEqual(self.api.trigger_count, 0)
        self.assertEqual(self.state(), "DIAGNOSE")
        with patch("orchestrator.load_profile", return_value={"ready": True, "profile": self.base.profile}):
            material = pinned_context(self.orch, self.run, "G8", input_hash)
        self.assertEqual(material["material_status"], "PINNED", material)
        text = "\n".join(material["summary"])
        self.assertIn("重新触发整条流水线", text)
        self.assertIn("100", text)
        self.assertIn("404", text)
        self.assertEqual(pinned_context(self.orch, self.run, "G8", "0" * 64)["material_status"], "MISSING")

    def test_b21_a_requested_rebuild_probe_404_is_not_authoritative_environment_evidence(self):
        # B-21 ③: a 404 probe proves only that the old build cannot be re-run from its
        # products. It stays in the G8 material and is not a failure-cause record.
        prefix = f"ipipe.dependency-failure:{self.run}:"
        self.assertTrue(self.plan_result()["ok"])
        protocol = self.orch.phase_protocol()
        self.assertIsNone(protocol._authoritative_failure_class(self.run))

        input_hash = self.request()

        self.assertEqual(self.orch.state.idempotency_results_with_prefix(prefix), [])
        self.assertIsNone(protocol._authoritative_failure_class(self.run))
        material = self.orch.state.idempotency_result(f"ipipe.g8-material:{self.run}:{input_hash}")
        self.assertEqual((material["probe"]["verdict"], material["probe"]["http_status"]),
                         ("UNAVAILABLE", 404))
        self.api.downloads["100"] = {"ok": False, "http_status": 403,
                                     "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED"}
        self.request()
        self.assertEqual(self.orch.state.idempotency_results_with_prefix(prefix), [])
        self.assertIsNone(protocol._authoritative_failure_class(self.run))

    def test_an_unapproved_hash_never_triggers(self):
        input_hash = self.request()

        pending = self.orch.approvals.request(
            "G8", input_hash, ["comate", "infoflow"], run_id=self.run,
            member_policy={"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]})
        for approval_id in ("missing-approval", pending["approval_id"]):
            with self.subTest(approval_id=approval_id):
                result = ipipe_rebuild.execute(
                    self.orch, self.runtime(), self.run, approval_id, input_hash)
                self.assertEqual(result["reason_code"], "APPROVAL_REQUIRED", result)
        self.assertEqual(self.api.trigger_count, 0)
        self.assertEqual(self.state(), "DIAGNOSE")

    def test_an_approval_for_another_hash_or_gate_never_triggers(self):
        input_hash = self.request()
        other = approved(self.orch.approvals, "G8", "f" * 64, self.run)
        wrong_gate = approved(self.orch.approvals, "G7", input_hash, self.run)
        for grant in (other, wrong_gate):
            with self.subTest(grant=grant):
                result = ipipe_rebuild.execute(
                    self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
                self.assertIn(result["reason_code"], {"APPROVAL_INPUT_MISMATCH", "APPROVAL_GATE_MISMATCH"})
        self.assertEqual(self.api.trigger_count, 0)

    def test_material_must_exist_for_the_hash(self):
        grant = self.approve("a" * 64)

        result = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], "a" * 64)

        self.assertEqual(result["reason_code"], "REBUILD_MATERIAL_MISSING")

    def test_justification_is_checked_again_at_the_moment_of_the_write(self):
        input_hash = self.request()
        grant = self.approve(input_hash)
        self.api.downloads["100"] = {"ok": True, "reason_code": "OK"}

        result = ipipe_rebuild.execute(
            self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)

        self.assertEqual(result["reason_code"], "REBUILD_NOT_JUSTIFIED", result)
        self.assertEqual(self.api.trigger_count, 0)

    def test_a_cr_that_moved_after_approval_never_triggers(self):
        input_hash = self.request()
        grant = self.approve(input_hash)
        self.live["42"] = "a2"

        result = ipipe_rebuild.execute(
            self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)

        self.assertEqual(result["reason_code"], "REVISION_DRIFT", result)
        self.assertEqual(self.api.trigger_count, 0)


class TriggerAndRebindTests(RebuildTestCase):
    def test_approved_rebuild_triggers_once_and_rebinds_the_run(self):
        self.new_build_on_trigger("101")
        before = self.orch.state.events(self.run)

        result = self.execute()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "101")
        self.assertEqual(self.api.trigger_count, 1)
        self.assertEqual(self.api.event_types, ["GERRIT_CHANGE"])
        self.assertEqual(self.state(), "IPIPE")
        event = self.orch.state.events(self.run)[-1]
        audit = event["payload"]["rebuild"]
        self.assertEqual((audit["original_build_id"], audit["build_id"], audit["attempt"]),
                         ("100", "101", 2))
        self.assertEqual(event["payload"]["previous_state"], "DIAGNOSE")
        self.assertEqual(event["payload"]["pipeline_plan"], self.plan)
        self.assertEqual(frozen_plan(self.orch.state.events(self.run)), self.plan)
        self.assertEqual(self.orch.state.events(self.run)[:len(before)], before)
        self.assertTrue(TransitionPolicy().validate("DIAGNOSE", "IPIPE")["allowed"])

    def test_replay_does_not_trigger_again(self):
        self.new_build_on_trigger("101")
        input_hash = self.request()
        grant = self.approve(input_hash)
        first = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
        events = len(self.orch.state.events(self.run))

        again = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
        direct = self.runtime().rebuild(
            self.base.profile, self.plan["revision_set"], grant, module=RUN_MODULE,
            original_build_id="100", attempt=2, reason="产物 404，无法重跑任何阶段")

        self.assertTrue(first["ok"] and again["ok"], again)
        self.assertEqual(again["reason_code"], "ALREADY_REBOUND")
        self.assertTrue(direct["ok"], direct)
        self.assertEqual(self.api.trigger_count, 1)
        self.assertEqual(len(self.orch.state.events(self.run)), events)

    def test_unparseable_response_is_reconciled_by_revision_and_time(self):
        self.new_build_on_trigger("101", minutes=1)
        self.api.trigger_result = {"unexpected": "shape"}

        result = self.execute()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "101")
        self.assertEqual(self.api.trigger_count, 1)
        observations = [
            item for item in self.orch.state.external_observations(self.run)
            if item["observation"].get("operation") == "ipipe.rebuild.response"
        ]
        self.assertEqual(observations[-1]["observation"]["response_keys"], ["unexpected"])
        self.assertIsNone(observations[-1]["observation"]["build_id"])

    def test_two_newer_candidates_are_ambiguous_and_nothing_is_guessed(self):
        def start_two():
            self.add_build("101", minutes=1)
            self.add_build("102", minutes=2)
        self.api.on_trigger = start_two
        self.api.trigger_result = {}

        result = self.execute()

        self.assertEqual(result["reason_code"], "BUILD_AMBIGUOUS", result)
        self.assertEqual(sorted(result["candidates"]), ["101", "102"])
        self.assertEqual(self.api.trigger_count, 1)
        self.assertEqual(self.state(), "DIAGNOSE")
        self.assertEqual(ipipe_rebuild.prepare_g8(
            self.orch, self.runtime(), self.run, reason="再来一次")["reason_code"], "QUERY_REQUIRED")
        intent = next(i for i in self.orch.state.all_intents(self.run) if "attempt-2" in i["idempotency_key"])
        approval = {"approval_id": intent["payload"]["approval_id"],
                    "input_hash": intent["payload"]["input_hash"]}
        settled = self.runtime().reconcile_rebuild(
            self.base.profile, self.plan["revision_set"], approval, module=RUN_MODULE,
            original_build_id="100", attempt=2, reason="产物 404，无法重跑任何阶段")
        self.assertEqual(settled["reason_code"], "BUILD_AMBIGUOUS", settled)
        self.assertEqual(sorted(settled["candidates"]), ["101", "102"])
        self.assertEqual(self.api.trigger_count, 1)

    def test_unknown_outcome_blocks_a_second_post_until_reconciled_by_get(self):
        self.api.trigger_result = {}
        input_hash = self.request()
        grant = self.approve(input_hash)
        first = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
        self.assertEqual(first["reason_code"], "REBUILD_RESPONSE_INVALID", first)

        again = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
        self.assertEqual(again["reason_code"], "QUERY_REQUIRED", again)
        self.assertEqual(self.api.trigger_count, 1)

        self.add_build("101", minutes=1)
        settled = ipipe_rebuild.execute(
            self.orch, self.runtime(), self.run, grant["approval_id"], input_hash, reconcile_only=True)
        self.assertTrue(settled["ok"], settled)
        self.assertEqual(settled["build_id"], "101")
        self.assertEqual(self.api.trigger_count, 1)
        self.assertEqual(self.state(), "IPIPE")

    def test_a_build_with_a_smaller_id_than_the_original_is_never_claimed(self):
        self.api.trigger_result = {}
        self.add_build("90", minutes=-300)
        input_hash = self.request()
        grant = self.approve(input_hash)

        first = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)

        self.assertEqual(first["reason_code"], "REBUILD_RESPONSE_INVALID", first)
        self.assertEqual(self.api.trigger_count, 1)
        self.assertEqual(self.state(), "DIAGNOSE")

    def test_a_build_that_started_before_the_intent_is_not_claimed_by_reconciliation(self):
        self.api.trigger_result = {}
        input_hash = self.request()
        grant = self.approve(input_hash)
        first = ipipe_rebuild.execute(self.orch, self.runtime(), self.run, grant["approval_id"], input_hash)
        self.assertEqual(first["reason_code"], "REBUILD_RESPONSE_INVALID", first)

        self.add_build("150", minutes=-600)
        settled = ipipe_rebuild.execute(
            self.orch, self.runtime(), self.run, grant["approval_id"], input_hash, reconcile_only=True)

        self.assertEqual(settled["reason_code"], "QUERY_REQUIRED", settled)
        self.assertEqual(self.state(), "DIAGNOSE")

    def test_a_newer_build_that_already_exists_is_followed_instead_of_triggering_a_third(self):
        self.add_build("150", minutes=-30)

        result = self.execute()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "150")
        self.assertEqual(self.api.trigger_count, 0)
        receipt = self.orch.state.result_by_idempotency_key(
            f"ipipe.trigger:{self.run}:{self.target['pipeline_id']}:"
            f"{self.plan['revision_set']['revision_set_id']}:attempt-2")
        self.assertTrue(receipt["receipt"]["response"]["adopted"])


class HistoryAndBindingTests(RebuildTestCase):
    def rebound(self):
        self.new_build_on_trigger("101", minutes=1)
        result = self.execute()
        self.assertTrue(result["ok"], result)
        return result

    def test_old_build_events_receipts_and_artifacts_are_left_as_they_were(self):
        state = self.orch.state
        old_binding = state.idempotency_result(f"ipipe.build-binding:{self.run}:100")
        old_stage = state.idempotency_result(f"ipipe.stage-binding:{self.run}:100-unit")
        old_ingest = state.idempotency_result(f"ipipe-ingest:{self.run}:100")
        old_artifacts = [a["artifact_id"] for a in self.orch.artifacts.phase_artifacts(self.run, "IPIPE")]
        old_events = state.events(self.run)
        self.assertIsNotNone(old_stage)

        self.rebound()

        self.assertEqual(state.idempotency_result(f"ipipe.build-binding:{self.run}:100"), old_binding)
        self.assertEqual(state.idempotency_result(f"ipipe.stage-binding:{self.run}:100-unit"), old_stage)
        self.assertEqual(state.idempotency_result(f"ipipe-ingest:{self.run}:100"), old_ingest)
        self.assertEqual(
            [a["artifact_id"] for a in self.orch.artifacts.phase_artifacts(self.run, "IPIPE")],
            old_artifacts)
        self.assertEqual(state.events(self.run)[:len(old_events)], old_events)
        marker = state.idempotency_result(f"ipipe.build-superseded:{self.run}:100")
        self.assertEqual((marker["build_id"], marker["superseded_by"], marker["attempt"]), ("100", "101", 2))

    def test_the_pending_diagnose_job_is_superseded_not_deleted_and_cannot_be_fulfilled(self):
        self.rebound()

        job = self.orch.state.producer_job("producer:diagnose")

        self.assertEqual(job["status"], "SUPERSEDED")
        self.assertEqual(job["payload"]["phase"], "DIAGNOSE")
        self.assertEqual(self.orch.state.pending_producer_jobs(self.run), [])
        with self.assertRaisesRegex(ValueError, "PRODUCER_JOB_SUPERSEDED"):
            self.orch.state.fulfill_producer_job("producer:diagnose", {"late": "draft"})
        with self.assertRaisesRegex(ValueError, "PRODUCER_JOB_NOT_PENDING"):
            self.orch.state.claim_producer_job(self.run, "producer:diagnose", "agent-1")

    def test_discover_and_monitor_follow_the_new_build(self):
        self.rebound()
        runtime = self.runtime()

        found = runtime.discover(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        monitored = runtime.monitor_once(
            found["build_id"], (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

        self.assertEqual(found["build_id"], "101", found)
        self.assertEqual(monitored["build_id"], "101")
        self.assertIn(monitored["status"], {"MONITORING", "MANUAL_WAIT"})
        stage_ids = {s["stage_build_id"] for s in monitored["stages"]}
        self.assertEqual(stage_ids, {"101-compile", "101-unit"})
        for stage_id in stage_ids:
            self.assertIsNotNone(
                self.orch.state.idempotency_result(f"ipipe.stage-binding:{self.run}:{stage_id}"))

    def test_the_worker_controller_follows_the_new_build_not_ambiguity(self):
        self.rebound()
        action = self.base.protocol.next(self.run)
        self.assertTrue(action["ok"], action)
        self.assertEqual(action["state"], "IPIPE")

        with patch.object(self.orch, "ipipe_runtime", return_value=self.runtime()):
            result = worker_driver._execute_ipipe(
                self.orch, self.run, action, self.api, self.base.knowledge)

        self.assertEqual(result.get("build_id"), "101", result)
        self.assertNotEqual(result.get("reason_code"), "IPIPE_ADOPT_FAILED")

    def test_watch_ipipe_polls_the_new_build_only(self):
        self.rebound()
        from ipipe_watch import IpipeWatcher
        watcher = IpipeWatcher.__new__(IpipeWatcher)
        watcher.orchestrator = self.orch

        self.assertEqual(watcher._build_ids(self.run), ["101"])

    def test_a_stage_of_the_superseded_build_cannot_be_rerun(self):
        # B-05 / I-1: the old build's stages stay bound for audit, but a G8 for one of
        # them would run a stage of a build the run no longer follows.
        self.rebound()
        runtime = self.runtime()

        hashed = runtime.rerun_input_hash("100-unit")
        direct = runtime.rerun("100-unit", {"approval_id": "any", "input_hash": "0" * 64})

        self.assertEqual(hashed["reason_code"], "STAGE_BUILD_SUPERSEDED", hashed)
        self.assertEqual(direct["reason_code"], "STAGE_BUILD_SUPERSEDED", direct)
        self.assertFalse(any(call[0] == "manual_execute_stage" for call in self.api.calls))

    def test_a_mismatching_trigger_kind_keeps_the_receipt_but_refuses_to_follow(self):
        self.new_build_on_trigger("101", minutes=1, ci="MERGE")

        result = self.execute()

        self.assertEqual(result["reason_code"], "TRIGGER_MODE_MISMATCH", result)
        self.assertEqual(self.state(), "DIAGNOSE")
        self.assertEqual(self.api.trigger_count, 1)
        self.assertIsNone(self.orch.state.idempotency_result(f"ipipe.build-superseded:{self.run}:100"))
        self.assertEqual(self.orch.state.producer_job("producer:diagnose")["status"], "PENDING")

    def test_a_rejected_trigger_kind_leaves_a_way_forward(self):
        # A-04: the mismatched build is bound to the run by its receipt; unless it is
        # marked rejected every reader sees two owned builds and the run is stuck.
        self.new_build_on_trigger("101", minutes=1, ci="MERGE")
        result = self.execute()
        self.assertEqual(result["reason_code"], "TRIGGER_MODE_MISMATCH", result)
        self.assertTrue(result["build_rejected"])
        self.assertIn("ipipe-rebuild", result["next_step"])

        marker = self.orch.state.idempotency_result(f"ipipe.build-rejected:{self.run}:101")
        current = ipipe_rebuild.current_build(self.orch, self.run, self.plan, RUN_MODULE)
        found = self.runtime().discover(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        from ipipe_watch import IpipeWatcher
        watcher = IpipeWatcher.__new__(IpipeWatcher)
        watcher.orchestrator = self.orch
        again = self.plan_result()

        self.assertEqual((marker["build_id"], marker["reason_code"]), ("101", "TRIGGER_MODE_MISMATCH"))
        self.assertEqual(current.get("build_id"), "100", current)
        self.assertEqual(found.get("build_id"), "100", found)
        self.assertNotIn("101", watcher._build_ids(self.run))
        self.assertTrue(again["ok"], again)
        self.assertEqual((again["attempt"], again["original_build_id"]), (3, "100"))
        # The receipt of the rejected attempt is kept for audit.
        self.assertIsNotNone(self.orch.state.result_by_idempotency_key(
            f"ipipe.trigger:{self.run}:{self.target['pipeline_id']}:"
            f"{self.plan['revision_set']['revision_set_id']}:attempt-2"))

    def test_a_rebind_without_the_g8_evidence_is_refused(self):
        self.new_build_on_trigger("101", minutes=1)
        input_hash = self.request()
        grant = self.approve(input_hash)
        material = self.orch.state.idempotency_result(ipipe_rebuild.material_key(self.run, input_hash))
        receipt = self.runtime().rebuild(
            self.base.profile, self.plan["revision_set"], grant, module=RUN_MODULE,
            original_build_id="100", attempt=2, reason=material["binding"]["reason"])
        self.assertTrue(receipt["ok"], receipt)

        refused = ipipe_rebuild.rebind(
            self.orch, self.runtime(), self.run, receipt, material, "no-such-approval", input_hash)

        self.assertEqual(refused["reason_code"], "APPROVAL_REQUIRED", refused)
        self.assertEqual(self.state(), "DIAGNOSE")


class LocalErrorTests(RebuildTestCase):
    """A rebuild write that fails at the call site leaves no trigger intent hanging."""

    def call_site_failure(self):
        # Wrong arity, with the pre-claim signature check defeated: the call itself
        # raises TypeError before trigger_by_revision runs, so no request exists.
        self.api.trigger_by_revision = lambda only_one: None
        with patch("clients.ipipe_runtime.inspect.signature", side_effect=ValueError):
            return self.execute()

    def trigger_intents(self):
        return [item for item in self.orch.state.all_intents(self.run)
                if item["operation"] == "ipipe.trigger"]

    def test_a_call_site_error_closes_the_trigger_intent_as_proven_local(self):
        result = self.call_site_failure()

        self.assertEqual(result["reason_code"], "REBUILD_LOCAL_ERROR", result)
        self.assertTrue(result["intent_closed"])
        (intent,) = self.trigger_intents()
        receipt = self.orch.state.result_by_idempotency_key(intent["idempotency_key"])
        self.assertIsNotNone(receipt, "trigger intent left without a receipt")
        self.assertEqual(receipt["receipt"]["response"]["reason_code"], "INTENT_ABANDONED")
        self.assertIs(receipt["receipt"]["response"]["local_error_proven"], True)
        self.assertEqual(self.orch.state.pending_intents(self.run), [])
        self.assertEqual(self.api.trigger_count, 0)
        # The run is not stuck behind an unknown outcome: the next request is attempt 3.
        del self.api.trigger_by_revision
        planned = self.plan_result()
        self.assertTrue(planned["ok"], planned)
        self.assertEqual(planned["attempt"], 3)

    def test_an_unrecordable_proof_is_reported_not_swallowed(self):
        with patch.object(self.orch.state, "abandon_intent",
                          side_effect=ValueError("LOCAL_ERROR_NOT_PROVEN")):
            result = self.call_site_failure()

        self.assertEqual(result["reason_code"], "REBUILD_LOCAL_ERROR_UNRECORDED", result)
        self.assertEqual(result["abandon_error"], "LOCAL_ERROR_NOT_PROVEN")
        self.assertIs(result["retry_allowed"], False)
        self.assertEqual(len(self.orch.state.pending_intents(self.run)), 1)

    def test_a_trigger_intent_needs_a_rebuild_write_error_proven_before_dispatch(self):
        from state_store import _local_error_observed
        intent = {"operation": "ipipe.trigger"}
        proven = {"operation": "ipipe.rebuild.write_error", "error_type": "TypeError",
                  "before_dispatch": True}

        def seen(*observations):
            return [{"observation": item} for item in observations]

        self.assertTrue(_local_error_observed(intent, seen(proven)))
        self.assertFalse(_local_error_observed(intent, seen({**proven, "before_dispatch": False})))
        self.assertFalse(_local_error_observed(intent, seen({k: v for k, v in proven.items()
                                                              if k != "before_dispatch"})))
        self.assertFalse(_local_error_observed(intent, seen({**proven, "http_status": 500})))
        self.assertFalse(_local_error_observed(
            intent, seen(proven, {"operation": "ipipe.rebuild.response"})))
        self.assertFalse(_local_error_observed(
            intent, seen({**proven, "operation": "ipipe.rerun.write_error"})))


class AttemptKeyTests(RebuildTestCase):
    def legacy_key(self):
        return (f"ipipe.trigger:{self.run}:{self.target['pipeline_id']}:"
                f"{self.plan['revision_set']['revision_set_id']}")

    def legacy_trigger(self):
        """The historic path: a G7-approved `trigger` that adopts the existing build."""
        runtime = self.runtime()
        hashed = runtime.trigger_input_hash(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        grant = approved(self.orch.approvals, "G7", hashed["input_hash"], self.run)
        result = runtime.trigger(self.base.profile, self.plan["revision_set"], grant, RUN_MODULE)
        self.assertEqual(result["build_id"], "100", result)
        return result

    def test_the_historic_key_is_attempt_one_and_is_left_alone(self):
        legacy = self.legacy_trigger()
        before = self.orch.state.result_by_idempotency_key(self.legacy_key())
        self.new_build_on_trigger("101", minutes=1)

        result = self.execute()

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.orch.state.result_by_idempotency_key(self.legacy_key()), before)
        rebuilt = self.orch.state.result_by_idempotency_key(f"{self.legacy_key()}:attempt-2")
        self.assertEqual(rebuilt["receipt"]["response"]["build_id"], "101")
        self.assertEqual(legacy["build_id"], "100")

    def test_replaying_the_historic_trigger_now_answers_with_the_rebuilt_build(self):
        self.legacy_trigger()
        self.new_build_on_trigger("101", minutes=1)
        self.assertTrue(self.execute()["ok"])

        replay = self.legacy_trigger_replay()

        self.assertEqual(replay["build_id"], "101", replay)
        self.assertEqual(self.api.trigger_count, 1)

    def legacy_trigger_replay(self):
        runtime = self.runtime()
        hashed = runtime.trigger_input_hash(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        grant = next(
            {"approval_id": row["approval_id"], "input_hash": hashed["input_hash"]}
            for row in self.orch.approvals.for_run(self.run)
            if row["action"] == "G7" and row["input_hash"] == hashed["input_hash"])
        return runtime.trigger(self.base.profile, self.plan["revision_set"], grant, RUN_MODULE)

    def test_a_second_rebuild_from_ipipe_takes_the_next_attempt_and_supersedes_the_first(self):
        self.new_build_on_trigger("101", minutes=1)
        self.assertTrue(self.execute()["ok"])
        self.assertEqual(self.state(), "IPIPE")
        self.new_build_on_trigger("102", minutes=2)

        planned = self.plan_result()
        self.assertTrue(planned["ok"], planned)
        self.assertEqual((planned["attempt"], planned["original_build_id"]), (3, "101"))
        result = self.execute(self.request())

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "102")
        self.assertEqual(self.api.trigger_count, 2)
        event = self.orch.state.events(self.run)[-1]
        self.assertEqual((event["state"], event["payload"]["previous_state"]), ("IPIPE", "IPIPE"))
        for superseded in ("100", "101"):
            self.assertIsNotNone(
                self.orch.state.idempotency_result(f"ipipe.build-superseded:{self.run}:{superseded}"))
        found = self.runtime().discover(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        self.assertEqual(found["build_id"], "102")

    def test_a_stale_attempt_number_is_refused_without_a_write(self):
        runtime = self.runtime()
        reason = "产物 404"
        hashed = runtime.rebuild_input_hash(
            self.base.profile, self.plan["revision_set"], RUN_MODULE,
            original_build_id="100", attempt=5, reason=reason)
        grant = self.approve(hashed["input_hash"])

        result = runtime.rebuild(
            self.base.profile, self.plan["revision_set"], grant, module=RUN_MODULE,
            original_build_id="100", attempt=5, reason=reason)

        self.assertEqual(result["reason_code"], "REBUILD_ATTEMPT_STALE", result)
        self.assertEqual(result["expected_attempt"], 2)
        self.assertEqual(self.api.trigger_count, 0)

    def test_an_unresolved_trigger_intent_of_any_attempt_blocks_a_new_request(self):
        runtime = self.runtime()
        context = runtime._context(self.base.profile, self.plan["revision_set"], RUN_MODULE)
        self.orch.state.claim_intent(self.run, "ipipe.trigger", self.legacy_key(), {"unresolved": True})

        refused = self.plan_result()

        self.assertEqual(refused["reason_code"], "QUERY_REQUIRED", refused)
        self.assertEqual(runtime.next_rebuild_attempt(context), 2)
        self.assertEqual(self.api.trigger_count, 0)

    def test_unknown_event_type_is_refused(self):
        self.assertEqual(self.plan_result(event_type="MERGE")["reason_code"], "REBUILD_EVENT_TYPE_INVALID")


class SpecAndCliTests(unittest.TestCase):
    def test_the_diagnose_to_ipipe_edge_exists_only_with_g8_evidence(self):
        self.assertIn("IPIPE", workflow_spec.allowed_transitions()["DIAGNOSE"])
        self.assertEqual(
            workflow_spec.TRANSITION_REQUIREMENTS[("DIAGNOSE", "IPIPE")], (("ipipe-rebuild",), "G8"))
        self.assertTrue(TransitionPolicy().validate("DIAGNOSE", "IPIPE")["allowed"])
        self.assertFalse(TransitionPolicy().validate("DIAGNOSE", "RELEASE")["allowed"])
        from evidence_policy import missing_evidence
        context = {"run_id": "r", "input_hash": "h", "approved_input_hash": "h",
                   "artifacts": ["ipipe-rebuild"], "approval_id": "a",
                   "approval_record": {"approval_id": "a", "run_id": "r", "action": "G7",
                                       "input_hash": "h", "effective_decision": "APPROVE"}}
        self.assertEqual(missing_evidence("IPIPE", context, "DIAGNOSE"), ("APPROVAL_REQUIRED", ["G8"]))
        context["approval_record"]["action"] = "G8"
        self.assertEqual(missing_evidence("IPIPE", context, "DIAGNOSE"), ("", []))

    def test_cli_requires_a_reason_and_keeps_request_and_reconcile_exclusive(self):
        for argv in (["ipipe-rebuild", "run-1"],
                     ["ipipe-rebuild", "run-1", "--reason", "r", "--request-g8", "--reconcile-only"],
                     ["ipipe-rebuild", "run-1", "--reason", "r", "--event-type", "MERGE"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    controller.main(["--config-root", "/nonexistent-root", *argv])
                self.assertEqual(raised.exception.code, 2)

    def test_cli_on_an_unknown_run_fails_closed_without_touching_the_platform(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()) as out:
            code = controller.main(["--config-root", root, "ipipe-rebuild", "no-such-run", "--reason", "r"])
        self.assertNotEqual(code, 0)
        self.assertFalse(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
