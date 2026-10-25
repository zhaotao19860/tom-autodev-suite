"""B-01: a CODE_ONLY repair re-enters IMPLEMENT on the approved Plan's baseline.

The repair round's DIAGNOSE action is bound to the failed candidate's revisions (r2).
IMPLEMENT must still measure the new Change Set from the Plan baseline (r1) and pin
that Plan, exactly like PLAN -> IMPLEMENT does; otherwise prepare_change_set and
validate_content refuse every repair as BASELINE_REVISION_MISMATCH.
"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artifact_store import ArtifactStore
from evidence_gate import EvidenceGate
from phase_protocol import PhaseProtocol
from state_store import StateStore
from transition_policy import TransitionPolicy
import test_phase_protocol_repair as _repair
from test_phase_protocol_repair import (
    BoundApprovalLedger,
    ProductionShapeKnowledgeSync,
    final_envelope,
)
from test_schema_validation import specialized_examples
import prepare_change_set


BASELINE = {"business": "r1", "tests": "t1"}
CANDIDATE = {"business": "r2", "tests": "t2"}
PROFILE = {"requirement_id": "BGW-1", "profile_hash": "a" * 64}


class _PrepareOrchestrator:
    """Just enough of Orchestrator for prepare_change_set's binding checks."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.state = protocol.state

    def next(self, run_id, read_only=False):
        return self.protocol.next(run_id, read_only=read_only)

    def phase_protocol(self):
        return self.protocol

    def _runtime_profile(self, run_id):
        # Stop right after the baseline binding checks; worktrees are out of scope.
        return {"ok": False, "reason_code": "PROFILE_STOP_FOR_TEST"}


class CodeOnlyRepairBaselineTests(unittest.TestCase):
    draft = _repair.PhaseProtocolRepairPublicationTests.draft

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.state = StateStore(root / "state.sqlite")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.approvals = BoundApprovalLedger()
        self.protocol = PhaseProtocol(
            state_store=self.state,
            artifact_store=self.artifacts,
            knowledge_sync=ProductionShapeKnowledgeSync(),
            approval_ledger=self.approvals,
            evidence_gate=EvidenceGate(),
            transition_policy=TransitionPolicy(),
        )

    def seed(self, run_id, phase, task_id, content, *, parent_hash=None, revisions=None):
        return self.artifacts.put_envelope(final_envelope(
            run_id, phase, task_id, content, parent_hash=parent_hash, revisions=revisions
        ))

    def plan_then_candidate(self, run_id):
        """PLAN(r1) accepted -> IMPLEMENT -> candidate Change Set at r2."""
        tasks = self.seed(run_id, "TASKS", None, specialized_examples()["task-dag"])
        plan = self.seed(
            run_id, "PLAN", "T-1", specialized_examples()["task-plan"],
            parent_hash=tasks["envelope"]["content_hash"], revisions=BASELINE,
        )
        self.state.transition(run_id, "IMPLEMENT", {
            **PROFILE, "previous_state": "PLAN", "task_id": "T-1",
            "artifact_id": plan["artifact_id"],
            "plan_artifact_id": plan["artifact_id"],
            "plan_content_hash": plan["envelope"]["content_hash"],
            "source_revisions": dict(BASELINE),
        })
        first = self.protocol.next(run_id)
        self.assertEqual(first["baseline_revisions"], BASELINE, first)
        change = specialized_examples()["change-set"]
        self.seed(run_id, "IMPLEMENT", "T-1", change,
                  parent_hash=plan["envelope"]["content_hash"], revisions=CANDIDATE)
        return plan, change

    def diagnose_code_only(self, run_id, *, ipipe_identity=None):
        action = self.protocol.next(run_id)
        self.assertTrue(action["ok"], action)
        self.assertEqual(action["state"], "DIAGNOSE")
        self.assertEqual(action["source_revisions"], CANDIDATE)
        diagnosis = specialized_examples()["diagnosis"]
        diagnosis.update({
            "route": "REPAIR", "repair_scope": "CODE_ONLY", "repair_diff_hash": None,
            "frozen_revisions": dict(CANDIDATE),
        })
        identity = ipipe_identity or {
            "build_id": None, "stage_id": None, "job_id": None, "environment_fingerprint": None,
        }
        diagnosis.update(identity)
        draft = self.draft(action, diagnosis, approval_id=f"approval-g6-{run_id}")
        self.approvals.approve(
            approval_id=f"approval-g6-{run_id}", run_id=run_id, gate="G6",
            input_hash=draft["approval_input_hash"],
        )
        return self.protocol.complete(run_id, draft)

    def assert_implement_rebound_to_plan(self, run_id, plan, change):
        action = self.protocol.next(run_id)
        self.assertTrue(action["ok"], action)
        self.assertEqual(action["state"], "IMPLEMENT")
        self.assertEqual(action["baseline_revisions"], BASELINE)
        self.assertEqual(action["source_revisions"], BASELINE)
        self.assertEqual(action["parent_artifact_hash"], plan["envelope"]["content_hash"])
        entry = self.state.events(run_id)[-1]["payload"]
        self.assertEqual(entry["plan_artifact_id"], plan["artifact_id"])
        self.assertEqual(entry["plan_content_hash"], plan["envelope"]["content_hash"])

        checked = self.protocol.validate_content(action, copy.deepcopy(change))
        self.assertEqual(checked["reason_code"], "OK", checked)

        job_id = f"producer:{action['action_id']}"
        self.state.record_producer_job(
            run_id, job_id,
            {key: action.get(key) for key in (
                "action_id", "phase", "task_id", "source_event_id", "input_hash",
                "result_schema", "required_human_gate", "run_id", "parent_artifact_hash",
                "source_revisions", "baseline_revisions",
            )},
        )
        prepared = prepare_change_set.prepare(
            _PrepareOrchestrator(self.protocol), run_id, job_id, {
                "change_set_id": "CS-2", "task_id": "T-1",
                "test_ids": list(change["test_ids"]),
                "traceability_delta": copy.deepcopy(change["traceability_delta"]),
                "deviations": [],
            },
        )
        # Every Plan/baseline binding check passed; only the stubbed profile stops it.
        self.assertEqual(prepared["reason_code"], "PROFILE_STOP_FOR_TEST", prepared)

    def test_review_failure_code_only_repair_reenters_implement_on_plan_baseline(self):
        run_id = "run-code-only-review"
        plan, change = self.plan_then_candidate(run_id)
        self.state.transition(run_id, "REVIEW", {
            **PROFILE, "task_id": "T-1", "source_revisions": dict(CANDIDATE),
        })
        review_action = self.protocol.next(run_id)
        rejected = specialized_examples()["review"]
        rejected["verdict"] = "REJECT"
        rejected["change_set_hash"] = change["candidate_hash"]
        reviewed = self.protocol.complete(run_id, self.draft(review_action, rejected, approval_id=None))
        self.assertEqual(reviewed["state"], "DIAGNOSE", reviewed)

        repaired = self.diagnose_code_only(run_id)

        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual((repaired["state"], repaired["task_id"]), ("IMPLEMENT", "T-1"))
        self.assert_implement_rebound_to_plan(run_id, plan, change)

    def test_ipipe_failure_code_only_repair_reenters_implement_on_plan_baseline(self):
        run_id = "run-code-only-ipipe"
        plan, change = self.plan_then_candidate(run_id)
        evidence = specialized_examples()["ipipe-evidence"]
        evidence.update({
            "status": "FAILURE", "classification": "CODE", "failure_signature": "SIG-1",
            "stages": [{"stage_id": "unit", "status": "FAILURE", "job_ids": ["job-1"]}],
            "jobs": [{"job_id": "job-1", "status": "FAILURE", "evidence_refs": ["ipipe:build-1/job-1"]}],
        })
        self.seed(run_id, "IPIPE", None, evidence, revisions=CANDIDATE)
        self.state.transition(run_id, "DIAGNOSE", {
            **PROFILE, "previous_state": "IPIPE", "task_id": "T-1",
            "source_revisions": dict(CANDIDATE),
        })

        repaired = self.diagnose_code_only(run_id, ipipe_identity={
            "build_id": "build-1", "stage_id": "unit", "job_id": "job-1",
            "environment_fingerprint": "env-1",
        })

        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual((repaired["state"], repaired["task_id"]), ("IMPLEMENT", "T-1"))
        self.assert_implement_rebound_to_plan(run_id, plan, change)

    def test_plan_from_a_superseded_dag_is_not_reused(self):
        run_id = "run-code-only-stale-plan"
        self.plan_then_candidate(run_id)
        amended = specialized_examples()["task-dag"]
        amended["nodes"][0]["capability_slice"] += " amended"
        self.seed(run_id, "TASKS", None, amended)

        self.assertIsNone(self.protocol._accepted_plan(run_id, "T-1"))

    def test_plan_without_an_accepting_transition_is_not_reused(self):
        run_id = "run-code-only-unaccepted"
        tasks = self.seed(run_id, "TASKS", None, specialized_examples()["task-dag"])
        self.seed(
            run_id, "PLAN", "T-1", specialized_examples()["task-plan"],
            parent_hash=tasks["envelope"]["content_hash"], revisions=BASELINE,
        )

        self.assertIsNone(self.protocol._accepted_plan(run_id, "T-1"))

    # --- B-04: the runtime's failure class, not the diagnosis' label, guards REPAIR ---

    def environment_failure_run(self, run_id, runtime_class):
        self.plan_then_candidate(run_id)
        evidence = specialized_examples()["ipipe-evidence"]
        evidence.update({
            "status": "FAILURE", "classification": runtime_class, "failure_signature": "SIG-ENV",
            "stages": [{"stage_id": "unit", "status": "FAILURE", "job_ids": ["job-1"]}],
            "jobs": [{"job_id": "job-1", "status": "FAILURE", "evidence_refs": ["ipipe:build-1/job-1"]}],
        })
        stored = self.seed(run_id, "IPIPE", None, evidence, revisions=CANDIDATE)
        self.state.transition(run_id, "DIAGNOSE", {
            **PROFILE, "previous_state": "IPIPE", "task_id": "T-1",
            "artifact_id": stored["artifact_id"], "source_revisions": dict(CANDIDATE),
        })
        action = self.protocol.next(run_id)
        diagnosis = specialized_examples()["diagnosis"]
        diagnosis.update({
            # The model relabels the runtime's environment failure as a code defect.
            "classification": "CODE", "failure_signature": "SIG-ENV",
            "route": "REPAIR", "repair_scope": "CODE_ONLY", "repair_diff_hash": None,
            "frozen_revisions": dict(CANDIDATE), "build_id": "build-1", "stage_id": "unit",
            "job_id": "job-1", "environment_fingerprint": "env-1",
        })
        return action, diagnosis

    def test_relabelled_environment_failure_cannot_be_repaired(self):
        for runtime_class in ("ENV_UNSATISFIED", "PIPELINE_TRANSIENT", "ENV_TRANSIENT"):
            with self.subTest(runtime_class=runtime_class):
                run_id = f"run-env-{runtime_class.lower()}"
                action, diagnosis = self.environment_failure_run(run_id, runtime_class)

                checked = self.protocol.validate_content(action, diagnosis)
                target = self.protocol._completion_target(action, {"content": diagnosis})

                self.assertEqual(checked["reason_code"], "ENVIRONMENT_REPAIR_FORBIDDEN", checked)
                self.assertEqual(checked["authoritative_classification"], runtime_class)
                self.assertEqual(target, ("STOPPED", None, "ENVIRONMENT_REPAIR_FORBIDDEN"))

    # --- B-11 / B-04: strong evidence only; every code-changing route is refused ---

    def diagnosis_with_route(self, diagnosis, route):
        changed = copy.deepcopy(diagnosis)
        changed["route"] = route
        if route != "REPAIR":
            changed.pop("repair_scope", None)
        return changed

    def test_keyword_misclassifications_are_advisory_and_repairable(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        stage = {"stage_build_id": "unit", "name": "unit", "class": ""}
        for message in ("case test_syn_proxy_timeout assert failed", "network.c:12: error"):
            with self.subTest(message=message):
                job = _normalize_job({"id": "job-1", "jobName": "job", "status": "FAIL",
                                      "message": message})
                runtime_class = _classification([stage], [job], ["unit"])
                run_id = f"run-keyword-{abs(hash(message))}"
                action, diagnosis = self.environment_failure_run(run_id, runtime_class)

                checked = self.protocol.validate_content(action, diagnosis)
                target = self.protocol._completion_target(action, {"content": diagnosis})

                self.assertNotIn(runtime_class, {"ENVIRONMENT_FAILURE", "ENV_UNSATISFIED", "ENV_TRANSIENT"})
                self.assertIsNone(self.protocol._authoritative_failure_class(run_id))
                self.assertEqual(checked["reason_code"], "OK", checked)
                self.assertNotEqual(target[2], "ENVIRONMENT_REPAIR_FORBIDDEN")

    def test_a_proven_dependency_404_blocks_repair_and_spec(self):
        run_id = "run-dependency-404"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        recorded = self.record_stage_dependency(run_id, [404])
        self.assertEqual(recorded["classification"], "ENV_UNSATISFIED")
        for route in ("REPAIR", "SPEC"):
            with self.subTest(route=route):
                content = self.diagnosis_with_route(diagnosis, route)
                checked = self.protocol.validate_content(action, content)
                target = self.protocol._completion_target(action, {"content": content})
                self.assertEqual(checked["reason_code"], "ENVIRONMENT_REPAIR_FORBIDDEN", checked)
                self.assertEqual(target, ("STOPPED", None, "ENVIRONMENT_REPAIR_FORBIDDEN"))
        # GRILL (requirement re-clarification) also ends in new code; the schema has its
        # own preconditions for it, so only the completion route is asserted here.
        grill = self.diagnosis_with_route(diagnosis, "GRILL")
        self.assertEqual(self.protocol._completion_target(action, {"content": grill}),
                         ("STOPPED", None, "ENVIRONMENT_REPAIR_FORBIDDEN"))

    def test_b21_a_rebuild_probe_403_is_not_authoritative(self):
        # B-21 ③: the probe only proves the old build cannot be re-run from its products.
        run_id = "run-rebuild-probe"
        action, diagnosis = self.environment_failure_run(run_id, "CODE_FAILURE")
        self.state.save_idempotency_result(f"ipipe.g8-material:{run_id}:{'b' * 64}", {
            "operation": "ipipe.rebuild",
            "binding": {"original_build_id": "build-1", "module": "resolver"},
            "probe": {"verdict": "UNAVAILABLE", "http_status": 403},
        })
        import environment_evidence
        self.assertFalse(hasattr(environment_evidence, "record_probe_failure"))
        self.assert_routes_allowed(run_id, action, diagnosis)

    def test_a_structured_runner_offline_signal_is_authoritative(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        stage = {"stage_build_id": "unit", "name": "unit", "class": ""}
        offline = _normalize_job({"id": "j", "status": "FAIL", "agentStatus": "OFFLINE",
                                  "message": "case test_x assert failed"})
        queued = _normalize_job({"id": "j", "status": "FAIL", "errorType": "QUEUE_TIMEOUT"})
        self.assertEqual(_classification([stage], [offline], ["unit"]), "ENV_UNSATISFIED")
        self.assertEqual(_classification([stage], [queued], ["unit"]), "ENV_TRANSIENT")

        action, diagnosis = self.environment_failure_run(
            "run-runner-offline", _classification([stage], [offline], ["unit"]))
        self.assertEqual(self.protocol.validate_content(action, diagnosis)["reason_code"],
                         "ENVIRONMENT_REPAIR_FORBIDDEN")

    def test_route_spec_cannot_bypass_the_environment_guard(self):
        action, diagnosis = self.environment_failure_run("run-env-spec", "ENV_UNSATISFIED")
        spec = self.diagnosis_with_route(diagnosis, "SPEC")

        checked = self.protocol.validate_content(action, spec)
        target = self.protocol._completion_target(action, {"content": spec})

        self.assertEqual(checked["reason_code"], "ENVIRONMENT_REPAIR_FORBIDDEN", checked)
        self.assertEqual(checked["route"], "SPEC")
        self.assertEqual(target, ("STOPPED", None, "ENVIRONMENT_REPAIR_FORBIDDEN"))

    def test_stop_and_architecture_review_stay_available_for_environment_failures(self):
        action, diagnosis = self.environment_failure_run("run-env-allowed", "ENV_UNSATISFIED")
        for route in ("STOPPED", "ARCHITECTURE_REVIEW"):
            with self.subTest(route=route):
                content = self.diagnosis_with_route(diagnosis, route)
                self.assertEqual(self.protocol.validate_content(action, content)["reason_code"], "OK")
                self.assertEqual(self.protocol._completion_target(action, {"content": content})[0], route)

    def test_a_code_failure_classified_by_the_runtime_still_repairs(self):
        action, diagnosis = self.environment_failure_run("run-env-code", "CODE_FAILURE")

        self.assertEqual(self.protocol.validate_content(action, diagnosis)["reason_code"], "OK")

    def test_pipeline_transient_is_never_repairable(self):
        import repair_policy
        self.assertIn("PIPELINE_TRANSIENT", repair_policy.NON_REPAIRABLE_FAILURES)
        verdict = repair_policy.next_action([{
            "failure_class": "PIPELINE_TRANSIENT", "diagnosis_confirmed": True,
            "failure_signature": "S", "progress": True, "resolved": False,
        }])
        self.assertEqual(verdict, {"action": "STOP", "reason_code": "PIPELINE_TRANSIENT"})

    # --- B-11 / B-04 residue: regression set (a)-(g) -------------------------------------

    def assert_repair_allowed(self, run_id, runtime_class):
        action, diagnosis = self.environment_failure_run(run_id, runtime_class)
        checked = self.protocol.validate_content(action, diagnosis)
        target = self.protocol._completion_target(action, {"content": diagnosis})
        self.assertIsNone(self.protocol._authoritative_failure_class(run_id))
        self.assertEqual(checked["reason_code"], "OK", checked)
        self.assertNotEqual(target[2], "ENVIRONMENT_REPAIR_FORBIDDEN", target)

    def assert_code_routes_refused(self, run_id, action, diagnosis, expected_class):
        grill = dict(self.diagnosis_with_route(diagnosis, "GRILL"),
                     classification="USER_REQUIREMENT_CHANGED")
        for route, content in (("REPAIR", diagnosis), ("SPEC", self.diagnosis_with_route(diagnosis, "SPEC")),
                               ("GRILL", grill)):
            with self.subTest(route=route):
                checked = self.protocol.validate_content(action, content)
                self.assertEqual(checked["reason_code"], "ENVIRONMENT_REPAIR_FORBIDDEN", checked)
                self.assertTrue(checked["content_invalid"], checked)
                self.assertEqual(checked["authoritative_classification"], expected_class)
                self.assertEqual(self.protocol._completion_target(action, {"content": content}),
                                 ("STOPPED", None, "ENVIRONMENT_REPAIR_FORBIDDEN"))

    def test_b11_a_failed_case_named_timeout_is_a_repairable_test_failure(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        # A BGW manual stage name carries no keyword; only the job message decides.
        stage = {"stage_build_id": "p0", "name": "P0新case回归", "class": ""}
        job = _normalize_job({"id": "job-1", "jobName": "case", "status": "FAIL",
                              "message": "case test_syn_proxy_timeout assert failed"})
        runtime_class = _classification([stage], [job], [])

        self.assertEqual(runtime_class, "TEST_FAILURE")
        self.assert_repair_allowed("run-b11-a", runtime_class)

    def test_b11_network_or_timeout_in_compiler_output_is_not_authoritative(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        cases = (
            ({"stage_build_id": "c", "name": "compile", "class": ""}, "network.c:12: error: 'x' undeclared"),
            ({"stage_build_id": "c", "name": "compile", "class": ""}, "timeout.h:3: error: expected ';'"),
            ({"stage_build_id": "c", "name": "编译", "class": ""}, "network.c:12: error"),
        )
        for index, (stage, message) in enumerate(cases):
            with self.subTest(stage=stage["name"], message=message):
                job = _normalize_job({"id": "job-1", "jobName": "job", "status": "FAIL",
                                      "message": message})
                runtime_class = _classification([stage], [job], [])
                self.assertIn(runtime_class, {"CODE_FAILURE", "ENVIRONMENT_SUSPECTED"})
                self.assertNotIn(runtime_class, {"ENVIRONMENT_FAILURE", "ENV_UNSATISFIED", "ENV_TRANSIENT"})
                self.assert_repair_allowed(f"run-b11-b-{index}", runtime_class)

    def test_b11_every_exact_get_bgwagent_candidate_404_is_authoritative(self):
        from environment_evidence import proven_dependency_failure
        run_id = "run-b11-c"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        # The shape product_policy.select_product returns when every candidate failed.
        every_404 = {
            "ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "module": "baidu/sysip/bgwagent",
            "candidates": [{"build_id": "b-new", "http_status": 404}, {"build_id": "b-old", "http_status": 404}],
            "http_statuses": [404], "download_check": {"build_id": "b-old", "http_status": 404},
        }
        recorded = self.record_stage_dependency(run_id, result=every_404)

        self.assertEqual(recorded["classification"], "ENV_UNSATISFIED")
        stored = self.state.idempotency_result(
            f"ipipe.dependency-failure:{run_id}:build-1:unit:baidu/sysip/bgwagent:0001")
        self.assertEqual((stored["http_statuses"], stored["stage_build_id"], stored["stage_conf_id"],
                          stored["consumed_by"]), ([404], "unit", "conf-unit", ["get_bgwagent"]))
        self.assertEqual(proven_dependency_failure(self.state, run_id, "build-1", "unit")["classification"],
                         "ENV_UNSATISFIED")
        self.assert_code_routes_refused(run_id, action, diagnosis, "ENV_UNSATISFIED")

    def test_b11_a_candidate_without_a_proven_status_proves_nothing(self):
        run_id = "run-b11-c-partial"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        for candidates in (
            [{"build_id": "b-new", "http_status": 404}, {"build_id": "b-old", "reason_code": "IREPO_TOKEN_REQUIRED"}],
            [{"build_id": "b-new", "http_status": 404}, {"build_id": "b-old", "http_status": 401}],
        ):
            with self.subTest(candidates=candidates):
                recorded = self.record_stage_dependency(run_id, result={
                    "ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED",
                    "candidates": candidates, "http_statuses": [404]})
                self.assertIsNone(recorded["classification"])
                self.assertEqual(recorded["advisory"], "DOWNLOAD_STATUS_UNPROVEN")
        self.assert_routes_allowed(run_id, action, diagnosis)

    # --- B-21: dependency evidence is stage-bound, 403/404-only, latest attempt wins ---

    def record_stage_dependency(self, run_id, statuses=None, *, result=None, stage="unit",
                                consumed=("get_bgwagent",), declared=("get_bgwagent",)):
        from environment_evidence import record_dependency_attempt
        if result is None:
            result = {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED",
                      "candidates": [{"build_id": f"b-{index}", "http_status": status}
                                     for index, status in enumerate(statuses)]}
        return record_dependency_attempt(
            self.state, run_id, build_id="build-1", stage_build_id=stage,
            stage_conf_id=f"conf-{stage}", module="baidu/sysip/bgwagent",
            consumed_by=list(consumed), declared_parameters=list(declared), result=result,
            source="stage-parameter product download")

    def assert_routes_allowed(self, run_id, action, diagnosis):
        self.assertIsNone(self.protocol._authoritative_failure_class(run_id, diagnosis.get("stage_id")))
        for route in ("REPAIR", "SPEC"):
            with self.subTest(route=route):
                content = diagnosis if route == "REPAIR" else self.diagnosis_with_route(diagnosis, route)
                checked = self.protocol.validate_content(action, content)
                self.assertEqual(checked["reason_code"], "OK", checked)
                self.assertNotEqual(self.protocol._completion_target(action, {"content": content})[2],
                                    "ENVIRONMENT_REPAIR_FORBIDDEN")

    def two_stage_failure_run(self, run_id):
        """build-1 failed in `unit` (code) and in manual stage `p0`; diagnosis names `unit`."""
        self.plan_then_candidate(run_id)
        evidence = specialized_examples()["ipipe-evidence"]
        evidence.update({
            "status": "FAILURE", "classification": "TEST_FAILURE", "failure_signature": "SIG-ENV",
            "stages": [{"stage_id": "unit", "status": "FAILURE", "job_ids": ["job-1"]},
                       {"stage_id": "p0", "status": "FAILURE", "job_ids": ["job-2"]}],
            "jobs": [{"job_id": "job-1", "status": "FAILURE", "evidence_refs": ["ipipe:build-1/job-1"]},
                     {"job_id": "job-2", "status": "FAILURE", "evidence_refs": ["ipipe:build-1/job-2"]}],
        })
        stored = self.seed(run_id, "IPIPE", None, evidence, revisions=CANDIDATE)
        self.state.transition(run_id, "DIAGNOSE", {
            **PROFILE, "previous_state": "IPIPE", "task_id": "T-1",
            "artifact_id": stored["artifact_id"], "source_revisions": dict(CANDIDATE),
        })
        action = self.protocol.next(run_id)
        diagnosis = specialized_examples()["diagnosis"]
        diagnosis.update({
            "classification": "CODE", "failure_signature": "SIG-ENV",
            "route": "REPAIR", "repair_scope": "CODE_ONLY", "repair_diff_hash": None,
            "frozen_revisions": dict(CANDIDATE), "build_id": "build-1", "stage_id": "unit",
            "job_id": "job-1", "environment_fingerprint": "env-1",
        })
        return action, diagnosis

    def test_b21_another_stages_dependency_404_does_not_block_this_stages_code_repair(self):
        run_id = "run-b21-other-stage"
        action, diagnosis = self.two_stage_failure_run(run_id)
        self.record_stage_dependency(run_id, [404, 404], stage="p0")
        self.assert_routes_allowed(run_id, action, diagnosis)
        # The same proof does bind the stage it was recorded for.
        self.assertEqual(self.protocol._authoritative_failure_class(run_id, "p0"), "ENV_UNSATISFIED")

    def test_b21_a_5xx_is_advisory_and_never_blocks(self):
        run_id = "run-b21-5xx"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        for statuses in ([502, 503], [404, 503], [500]):
            with self.subTest(statuses=statuses):
                recorded = self.record_stage_dependency(run_id, statuses)
                self.assertIsNone(recorded["classification"])
                self.assertEqual(recorded["advisory"], "ENV_TRANSIENT_SUSPECTED")
        self.assert_routes_allowed(run_id, action, diagnosis)

    def test_b21_a_module_the_stage_does_not_consume_is_not_authoritative(self):
        run_id = "run-b21-not-consumed"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        self.record_stage_dependency(run_id, [404], consumed=(), declared=("get_bgw_test_case",))
        self.assert_routes_allowed(run_id, action, diagnosis)

    def test_b21_the_latest_attempt_wins(self):
        run_id = "run-b21-latest"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        first = self.record_stage_dependency(run_id, [404])
        later = self.record_stage_dependency(run_id, result={"ok": True, "product_url": "u"})
        self.assertEqual((first["attempt"], later["attempt"], later["outcome"]), (1, 2, "AVAILABLE"))
        self.assert_routes_allowed(run_id, action, diagnosis)
        # A newer 404 attempt proves it again; every attempt stays auditable.
        self.record_stage_dependency(run_id, [404])
        self.assertEqual(len(self.state.idempotency_results_with_prefix(
            f"ipipe.dependency-failure:{run_id}:build-1:unit:")), 3)
        self.assert_code_routes_refused(run_id, action, diagnosis, "ENV_UNSATISFIED")

    def test_b23_an_inconclusive_later_attempt_does_not_clear_a_proven_404(self):
        run_id = "run-b23"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        self.record_stage_dependency(run_id, [404])
        self.record_stage_dependency(run_id, [503])
        self.assert_code_routes_refused(run_id, action, diagnosis, "ENV_UNSATISFIED")
        self.record_stage_dependency(run_id, result={"ok": True, "product_url": "u"})
        self.assert_routes_allowed(run_id, action, diagnosis)

    def test_b21_a_diagnosis_cannot_dodge_by_naming_no_or_a_passing_stage(self):
        run_id = "run-b21-dodge"
        action, diagnosis = self.environment_failure_run(run_id, "TEST_FAILURE")
        self.record_stage_dependency(run_id, [404])
        self.assertEqual(self.protocol._authoritative_failure_class(run_id, None), "ENV_UNSATISFIED")
        self.assertEqual(self.protocol._authoritative_failure_class(run_id, "deploy"), "ENV_UNSATISFIED")

    def test_b11_structured_agent_offline_field_is_authoritative(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        stage = {"stage_build_id": "unit", "name": "unit", "class": ""}
        offline = _normalize_job({"id": "j", "status": "FAIL", "runnerStatus": "offline",
                                  "message": "case test_x assert failed"})
        runtime_class = _classification([stage], [offline], [])
        self.assertEqual(runtime_class, "ENV_UNSATISFIED")

        action, diagnosis = self.environment_failure_run("run-b11-e", runtime_class)
        self.assert_code_routes_refused("run-b11-e", action, diagnosis, "ENV_UNSATISFIED")

    def test_b11_free_text_never_yields_a_structured_class(self):
        from clients.ipipe_runtime import _classification, _normalize_job
        stage = {"stage_build_id": "x", "name": "deploy", "class": ""}
        for message in ("AGENT_OFFLINE", "agent offline", "runner lost: NO_AVAILABLE_AGENT", "QUEUE_TIMEOUT"):
            with self.subTest(message=message):
                job = _normalize_job({"id": "j", "jobName": message, "status": "FAIL", "message": message})
                self.assertNotIn(_classification([stage], [job], []), {"ENV_UNSATISFIED", "ENV_TRANSIENT"})

    def test_b11_a_legacy_keyword_environment_failure_is_advisory(self):
        run_id = "run-b11-f"
        action, diagnosis = self.environment_failure_run(run_id, "ENVIRONMENT_FAILURE")
        self.state.record_failure_case("SIG-ENV", "ENVIRONMENT_FAILURE", run_id)

        self.assertIsNone(self.protocol._authoritative_failure_class(run_id))
        self.assertEqual(self.protocol.validate_content(action, diagnosis)["reason_code"], "OK")
        self.assertNotEqual(self.protocol._completion_target(action, {"content": diagnosis})[2],
                            "ENVIRONMENT_REPAIR_FORBIDDEN")

    def test_b04_non_code_routes_stay_open_under_an_authoritative_class(self):
        action, diagnosis = self.environment_failure_run("run-b04-g", "ENV_UNSATISFIED")
        incomplete = dict(self.diagnosis_with_route(diagnosis, "DIAGNOSIS_INCOMPLETE"),
                          evidence_state="INSUFFICIENT", hypothesis=None, repair_direction=None,
                          repair_diff_hash=None, repair_plan=[])
        for route, content, expected in (
            ("STOPPED", self.diagnosis_with_route(diagnosis, "STOPPED"), ("STOPPED", None, "OK")),
            ("ARCHITECTURE_REVIEW", self.diagnosis_with_route(diagnosis, "ARCHITECTURE_REVIEW"),
             ("ARCHITECTURE_REVIEW", None, "OK")),
            ("DIAGNOSIS_INCOMPLETE", incomplete, ("STOPPED", None, "DIAGNOSIS_INCOMPLETE")),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.protocol.validate_content(action, content)["reason_code"], "OK")
                self.assertEqual(self.protocol._completion_target(action, {"content": content}), expected)
        spec = self.diagnosis_with_route(diagnosis, "SPEC")
        self.assertEqual(self.protocol.validate_content(action, spec)["reason_code"],
                         "ENVIRONMENT_REPAIR_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
