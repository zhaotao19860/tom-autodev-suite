"""Contract 3.1: the intent state machine and the iPipe manual-stage confirmation."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import intent_executor as ie
from approval_ledger import ApprovalLedger
from clients.ipipe_runtime import _normalize_stage
from state_store import StateStore
from tests.platform_fixtures import load, response
from worker_driver import _next_ready_manual_stage

from test_ipipe_runtime import FakeApi, approved, build, pinned_runtime


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "s.sqlite")
        claim = self.state.claim_exclusive_intent(
            "run-1", "ipipe.rerun", "ipipe.rerun:run-1:stage-1:a", {"x": 1},
            exclusive_prefix="ipipe.rerun:run-1:stage-1")
        self.intent = claim["intent"]

    def tearDown(self):
        self.temp.cleanup()

    def claim_again(self):
        return self.state.claim_exclusive_intent(
            "run-1", "ipipe.rerun", "ipipe.rerun:run-1:stage-1:b", {"x": 2},
            exclusive_prefix="ipipe.rerun:run-1:stage-1")

    def test_a_fresh_claim_is_claimed_and_holds_the_target(self):
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.CLAIMED)
        self.assertEqual(self.claim_again()["status"], "BLOCKED")

    def test_unknown_stays_unknown_and_keeps_holding(self):
        ie.settle(self.state, self.intent, ie.Verdict.unknown(reason="no start yet"))
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.UNKNOWN)
        self.assertEqual(self.claim_again()["status"], "BLOCKED")

    def test_ambiguous_is_sticky_and_holds_the_target(self):
        ie.settle(self.state, self.intent, ie.Verdict.ambiguous(candidates=["s2", "s3"]))
        ie.settle(self.state, self.intent, ie.Verdict.unknown())
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.AMBIGUOUS)
        self.assertEqual(self.claim_again()["status"], "BLOCKED")

    def test_confirmed_absent_frees_the_target_for_a_new_attempt(self):
        ie.settle(self.state, self.intent, ie.Verdict.absent(checked="listing"))
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.CONFIRMED_ABSENT)
        self.assertIn(ie.CONFIRMED_ABSENT, ie.RETRYABLE)
        self.assertEqual(self.claim_again()["status"], "CLAIMED")

    def test_a_receipt_confirms_and_frees(self):
        self.state.receipt(self.intent["intent_id"], {"ok": True, "reason_code": "OK"}, [])
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.CONFIRMED)
        self.assertEqual(self.claim_again()["status"], "CLAIMED")

    def test_an_operator_abandonment_is_not_a_retryable_absence(self):
        self.state.abandon_intent(self.intent["intent_id"], "operator", "tester")
        self.assertEqual(ie.intent_state(self.state, self.intent), ie.ABANDONED)
        self.assertNotIn(ie.ABANDONED, ie.RETRYABLE)

    def test_confirm_stops_at_the_first_decisive_read_and_never_more_than_polls(self):
        reads, sleeps = [], []
        answers = [ie.Verdict.unknown(), ie.Verdict.unknown(), ie.Verdict.confirmed({"ok": True})]
        verdict = ie.confirm(lambda: reads.append(1) or answers[len(reads) - 1],
                             polls=4, interval=5, sleeper=sleeps.append)
        self.assertEqual(verdict.state, ie.CONFIRMED)
        self.assertEqual((len(reads), sleeps), (3, [5, 5]))
        reads.clear()
        verdict = ie.confirm(lambda: reads.append(1) or ie.Verdict.unknown(),
                             polls=2, interval=1, sleeper=lambda _s: None)
        self.assertEqual((verdict.state, len(reads)), (ie.UNKNOWN, 2))


class ManualStageExecuteTests(unittest.TestCase):
    """The first operation on the executor: iPipe manual-stage execute."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")
        self.api = FakeApi()
        self.runtime = pinned_runtime(self.state, self.ledger, self.api, sleeper=lambda _s: None)
        manual = {"id": "stage-1", "stageName": "unit", "stageConfId": "c1", "status": "PENDING_FOR_USER"}
        item = build(status="RUNNING", stages=[manual])
        self.api.candidates = [item]
        self.api.builds["build-1"] = item
        self.runtime.discover(__import__("test_ipipe_runtime").PROFILE, __import__("test_ipipe_runtime").REVISIONS)
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        # The real gateway's answer to a manual execute (DERIVED from 5 recorded observations).
        self.api.rerun_result = response("derived-manual-execute-ack-without-identity")

    def tearDown(self):
        self.temp.cleanup()

    def intent(self):
        return next(i for i in self.state.all_intents("run-1") if i["operation"] == "ipipe.rerun")

    def test_the_recorded_ack_shape_has_no_stage_identity(self):
        record = load("derived-manual-execute-ack-without-identity")
        self.assertEqual(record["origin"], "DERIVED")
        self.assertEqual(sorted(record["response"]), ["code", "entities", "msg"])

    def test_an_identityless_ack_is_confirmed_by_the_stage_start(self):
        self.api.on_rerun = lambda: self.api.builds["build-1"].update(
            stageBuilds=[{"id": "stage-1", "stageName": "unit", "stageConfId": "c1", "status": "RUNNING"}])
        result = self.runtime.rerun("stage-1", self.approval)
        self.assertTrue(result["ok"], result)
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.CONFIRMED)

    def test_a_stage_that_never_starts_stays_unknown_and_blocks_a_second_g8(self):
        result = self.runtime.rerun("stage-1", self.approval)
        self.assertEqual(result["reason_code"], "RERUN_RESPONSE_INVALID")
        self.assertEqual(result["intent_state"], ie.UNKNOWN)
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.UNKNOWN)
        second = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        self.assertEqual(self.runtime.rerun("stage-1", second)["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_two_unclaimed_successors_are_ambiguous_and_never_retried(self):
        def two_successors():
            self.api.builds["build-1"].update(stageBuilds=[
                {"id": "stage-1", "stageName": "unit", "stageConfId": "c1", "status": "PENDING_FOR_USER"},
                {"id": "stage-2", "stageName": "unit", "stageConfId": "c1", "status": "RUNNING"},
                {"id": "stage-3", "stageName": "unit", "stageConfId": "c1", "status": "RUNNING"},
            ])
        self.api.on_rerun = two_successors
        result = self.runtime.rerun("stage-1", self.approval)
        self.assertEqual(result["reason_code"], "INTENT_AMBIGUOUS")
        self.assertEqual(result["candidates"], ["stage-2", "stage-3"])
        # Reconcile does not turn it into something else, and no second POST is possible.
        self.assertEqual(self.runtime.reconcile_rerun("stage-1", self.approval)["reason_code"],
                         "INTENT_AMBIGUOUS")
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.AMBIGUOUS)
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_reconcile_of_an_unknown_intent_confirms_once_the_stage_starts(self):
        self.runtime.rerun("stage-1", self.approval)
        self.api.builds["build-1"]["stageBuilds"][0]["status"] = "RUNNING"
        self.assertTrue(self.runtime.reconcile_rerun("stage-1", self.approval)["ok"])
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.CONFIRMED)


class TriggerTests(unittest.TestCase):
    """iPipe trigger on the executor: identity is the one build of the frozen revisions."""

    def setUp(self):
        import test_ipipe_runtime as base
        self.base = base
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")
        self.api = FakeApi()
        self.runtime = pinned_runtime(self.state, self.ledger, self.api, sleeper=lambda _s: None)
        self.approval = approved(self.ledger, "G7", base.canonical_hash(base.trigger_binding()))

    def tearDown(self):
        self.temp.cleanup()

    def trigger(self):
        return self.runtime.trigger(self.base.PROFILE, self.base.REVISIONS, self.approval)

    def intent(self):
        return next(i for i in self.state.all_intents("run-1") if i["operation"] == "ipipe.trigger")

    def posts(self):
        return sum(c[0] == "trigger_by_revision" for c in self.api.calls)

    def test_an_answer_without_a_build_id_is_confirmed_by_reading_the_revision_back(self):
        self.api.trigger_result = {"code": 0, "msg": "ok"}
        self.api.on_trigger = lambda: self.api.candidates.append(build("build-9"))
        self.api.builds["build-9"] = build("build-9")
        result = self.trigger()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "build-9")
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.CONFIRMED)
        self.assertEqual(self.posts(), 1)

    def test_two_builds_of_the_revisions_after_an_identityless_answer_are_ambiguous(self):
        self.api.trigger_result = {"code": 0, "msg": "ok"}

        def two_builds():
            for build_id in ("build-8", "build-9"):
                self.api.candidates.append(build(build_id))
                self.api.builds[build_id] = build(build_id)
        self.api.on_trigger = two_builds
        result = self.trigger()
        self.assertEqual(result["reason_code"], "BUILD_AMBIGUOUS", result)
        self.assertEqual(result["intent_state"], ie.AMBIGUOUS)
        self.assertEqual(result["candidates"], ["build-8", "build-9"])
        # Sticky: a replay neither adopts one of them nor posts again.
        again = self.trigger()
        self.assertEqual((again["reason_code"], again["candidates"]),
                         ("BUILD_AMBIGUOUS", ["build-8", "build-9"]))
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.AMBIGUOUS)
        self.assertEqual(self.posts(), 1)

    def test_an_unanswered_trigger_stays_unknown_without_a_second_post(self):
        self.api.trigger_result = TimeoutError("no answer")
        self.assertEqual(self.trigger()["reason_code"], "TRIGGER_RESULT_UNKNOWN")
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.UNKNOWN)
        self.assertEqual(self.trigger()["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(self.posts(), 1)

    def test_an_unanswered_trigger_whose_build_appears_later_is_confirmed_on_replay(self):
        self.api.trigger_result = TimeoutError("no answer")
        self.trigger()
        self.api.candidates.append(build("build-7"))
        self.api.builds["build-7"] = build("build-7")
        replay = self.trigger()
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(ie.intent_state(self.state, self.intent()), ie.CONFIRMED)
        self.assertEqual(self.posts(), 1)


class StagePlanTests(unittest.TestCase):
    """Contract 3.3: a manual-stage G8 binds the build's shape and the live configuration."""

    def setUp(self):
        import test_ipipe_runtime as base
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateStore(root / "state.sqlite")
        self.ledger = ApprovalLedger(root / "approvals.sqlite")
        self.api = FakeApi()
        self.api.pipeline = {**self.api.pipeline, "stages": [
            {"stageConfId": "c1", "stageName": "unit", "paramBeans": [], "groupJobs": [[
                {"jobConfId": "j1", "jobType": "AGILE_SHELL"}]]}]}
        self.runtime = pinned_runtime(self.state, self.ledger, self.api, sleeper=lambda _s: None)
        manual = {"id": "stage-1", "stageName": "unit", "stageConfId": "c1", "status": "PENDING_FOR_USER"}
        item = build(status="RUNNING", stages=[manual])
        self.api.candidates = [item]
        self.api.builds["build-1"] = item
        self.runtime.discover(base.PROFILE, base.REVISIONS)
        self.runtime.monitor("build-1", (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

    def tearDown(self):
        self.temp.cleanup()

    def test_the_g8_binds_both_snapshots(self):
        bound = self.runtime.rerun_input_hash("stage-1")
        self.assertEqual(set(bound["binding"]["stage_plan"]), {"build_hash", "config_hash"})

    def test_a_configuration_edited_after_approval_voids_it(self):
        approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        self.api.pipeline["stages"][0]["paramBeans"] = [{"name": "get_bgwagent", "required": False}]
        result = self.runtime.rerun("stage-1", approval)
        self.assertFalse(result["ok"])
        self.assertFalse(any(c[0] == "manual_execute_stage" for c in self.api.calls))

    def test_an_unchanged_pipeline_runs_under_the_approval(self):
        approval = approved(self.ledger, "G8", self.runtime.rerun_input_hash("stage-1")["input_hash"])
        self.runtime.rerun("stage-1", approval)
        self.assertEqual(sum(c[0] == "manual_execute_stage" for c in self.api.calls), 1)

    def test_config_hash_is_stable_and_ignores_unrelated_fields(self):
        import stage_plan
        conf = response("bgw1995-branch-pipeline-conf-348101")
        noisy = {**conf, "stages": [{**s, "lastModifiedUser": "someone"} for s in conf["stages"]]}
        self.assertEqual(stage_plan.config_hash(conf), stage_plan.config_hash(noisy))
        edited = {**conf, "stages": [
            {**s, "paramBeans": []} if s["stageName"] == "自动化回归测试" else s
            for s in conf["stages"]]}
        self.assertNotEqual(stage_plan.config_hash(conf), stage_plan.config_hash(edited))


class EarlyReleaseFixtureTests(unittest.TestCase):
    def test_release_shown_pending_while_regression_runs_is_not_the_stage_to_drive(self):
        stages = [_normalize_stage(s) for s in
                  response("derived-branch-release-pending-while-regression-runs")]
        self.assertEqual(stages[-1]["status"], "PENDING_FOR_USER")
        self.assertIsNone(_next_ready_manual_stage(stages))


if __name__ == "__main__":
    unittest.main()
