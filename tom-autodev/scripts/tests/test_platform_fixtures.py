"""Controller behaviour against recorded BGW-1995 iPipe responses."""

import tempfile
import unittest
from pathlib import Path

import stage_parameters
from approval_ledger import ApprovalLedger
from clients.ipipe_client import IpipeTransportError
from clients.ipipe_runtime import (
    IpipeRuntime, _build_passed, _job_skip_reasons, _normalize_stage,
)
from state_store import StateStore
from tests.platform_fixtures import all_records, response
from worker_driver import _next_ready_manual_stage

REVISION = "a9715d5bc29cd61e6d4f5feb38d36061ea16da4f"
BRANCH = "x86bgw-dpdk-2011-syncookie"


class ReplayApi:
    """Serves recorded responses; anything not recorded raises like a missing object."""

    def __init__(self):
        self.stages = {"275535376": "bgw1995-change-275535376-stages",
                       "275700446": "bgw1995-branch-275700446-stages-final"}
        self.details = {"649615692": "bgw1995-change-p0newcase-expression-skip",
                        "649615694": "bgw1995-change-system-test-expression-skip",
                        "649615686": "bgw1995-change-unit-partial-skip",
                        "649615688": "bgw1995-change-p0-regression-partial-skip",
                        "649960670": "bgw1995-branch-regression-detail",
                        "649960671": "bgw1995-branch-config-deleted-stage-detail",
                        "649960675": "bgw1995-branch-release-detail"}

    def pipeline_stage_info(self, build_id):
        return response(self.stages[str(build_id)])

    def stage_detail(self, stage_id):
        if str(stage_id) not in self.details:
            return {"code": 0, "msg": "", "entities": {"stageBuild": {}, "realJobBuilds": []}}
        return response(self.details[str(stage_id)])

    def get_pipeline_by_id(self, pipeline_id, brief=True):
        return response("bgw1995-branch-pipeline-conf-348101")

    def builds_by_revision(self, module, revision, pipeline_id):
        return response("bgw1995-branch-builds-by-revision")

    def pipelines_by_module(self, module):
        # Not recorded: the module listing names 348101 as its BranchPipeline (profile fact).
        return [{"id": "348101", "pipelineName": "BranchPipeline"}]

    def release_info(self, module, branch):
        error = response("gateway-get-release-info-404")["error"]
        raise IpipeTransportError(error["reason_code"], status=error["status"], transient=False)


class FixtureIntegrityTests(unittest.TestCase):
    def test_every_fixture_matches_its_recorded_hash_and_names_its_origin(self):
        records = all_records()
        self.assertGreaterEqual(len(records), 9)
        for record in records:
            self.assertEqual(record["schema"], "platform-fixture.v1")
            self.assertIn(record["origin"], {"RECORDED", "DERIVED"})

    def test_no_person_or_credential_reached_the_public_repository(self):
        text = "".join(str(record["response"]) for record in all_records())
        for marker in ("@baidu.com", "IREPO-TOKEN:", "triggerUser", "bjkjy-"):
            self.assertNotIn(marker, text)


class RecordedChangePipelineTests(unittest.TestCase):
    def test_expression_skipped_stages_do_not_fail_the_change_pipeline(self):
        # 348102 for dpdk-2011 branches skips P0新case回归 by expression (3a50d6e).
        # A stage whose jobs mix EXECUTE and EXPRESSION (单测, p0级自动化回归测试) ran and
        # must pass on its own; only the wholly expression-skipped ones are set aside.
        api = ReplayApi()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        runtime = IpipeRuntime(StateStore(root / "s.sqlite"), ApprovalLedger(root / "a.sqlite"),
                               "run-1", api, validated_profile={})
        stages = runtime._mark_expression_skips(
            [_normalize_stage(s) for s in api.pipeline_stage_info("275535376")])
        marked = {s["name"] for s in stages if s.get("skipped_by_expression")}
        self.assertEqual(marked, {"P0新case回归", "系统测试p0级功能回归"})
        self.assertTrue(_build_passed("SKIPPED", stages))


class RecordedBranchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.api = ReplayApi()

    def test_a_partly_expression_skipped_stage_is_not_wholly_expression_skipped(self):
        reasons = _job_skip_reasons(self.api.stage_detail("649960670"))
        self.assertIn("EXECUTE", reasons)
        self.assertIn("EXPRESSION", reasons)

    def test_regression_declares_get_bgwagent_in_config_not_in_manual_params(self):
        detail = self.api.stage_detail("649960670")
        stage = detail["entities"]["stageBuild"]
        self.assertIsNone(stage["manualParams"])
        declared = stage_parameters.dependency_declaration(
            stage, detail["entities"], self.api.get_pipeline_by_id("348101", brief=False))
        self.assertTrue(declared["ok"], declared)
        self.assertIn("get_bgwagent", declared["names"])

    def test_a_stage_deleted_from_live_config_is_explicitly_parameterless_in_the_build(self):
        # QA removed 系统测试功能回归 (5993070) from 348101 while 275700446 ran.
        detail = self.api.stage_detail("649960671")
        stage = detail["entities"]["stageBuild"]
        conf_ids = {str(s["stageConfId"]) for s in
                    self.api.get_pipeline_by_id("348101", brief=False)["stages"]}
        self.assertNotIn(str(stage["stageConfId"]), conf_ids)
        self.assertEqual(stage_parameters.declared_names(stage)["names"], [])
        # Contract 3.3: the build's own explicit empty declaration is used, and flagged.
        declared = stage_parameters.dependency_declaration(
            stage, detail["entities"], self.api.get_pipeline_by_id("348101"))
        self.assertTrue(declared["ok"], declared)
        self.assertEqual(declared["names"], [])
        self.assertTrue(declared["config_stage_missing"])
        self.assertEqual(declared["configuration_source"], "build_snapshot")
        # Without an explicit empty declaration it stays unknown.
        unknown = {**stage, "hasDynamicParams": True}
        self.assertEqual(stage_parameters.dependency_declaration(
            unknown, detail["entities"], self.api.get_pipeline_by_id("348101"))["reason_code"],
            "STAGE_PARAMETERS_UNKNOWN")

    def test_release_identity_comes_from_the_agile_release_job(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        runtime = IpipeRuntime(StateStore(root / "s.sqlite"), ApprovalLedger(root / "a.sqlite"),
                               "run-1", self.api, validated_profile={})
        found = runtime._branch_release({
            "module": "baidu/sysip/x86bgw", "target_branch": BRANCH,
            "revision_map": {"baidu/sysip/x86bgw": REVISION},
        })
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["version"], "26.10.20.1")
        self.assertEqual(found["branch_build_id"], "275700446")
        self.assertTrue(found["product_url"].endswith("/releases/26.10.20.1/files"))

    def test_two_releases_of_the_same_revision_are_ambiguous(self):
        # DERIVED from the recorded release detail: a second AGILE_RELEASE of a9715d5.
        import copy
        recorded = response("bgw1995-branch-release-detail")
        second = copy.deepcopy(recorded["entities"]["realJobBuilds"][0][0])
        second["realJobBuild"]["id"] = 15480417
        second["realJobBuild"]["version"] = "26.10.20.2"
        recorded["entities"]["realJobBuilds"][0].append(second)
        self.api.details["649960675"] = None
        original = self.api.stage_detail
        self.api.stage_detail = lambda stage_id: (
            recorded if str(stage_id) == "649960675" else original(stage_id))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        runtime = IpipeRuntime(StateStore(root / "s.sqlite"), ApprovalLedger(root / "a.sqlite"),
                               "run-1", self.api, validated_profile={})
        found = runtime._branch_release({
            "module": "baidu/sysip/x86bgw", "target_branch": BRANCH,
            "revision_map": {"baidu/sysip/x86bgw": REVISION},
        })
        self.assertEqual(found["reason_code"], "RELEASE_AMBIGUOUS", found)
        self.assertEqual(found["candidates"], ["15480416", "15480417"])

    def test_the_release_listing_endpoint_is_absent_on_the_gateway(self):
        with self.assertRaises(IpipeTransportError) as raised:
            self.api.release_info("baidu/sysip/x86bgw", BRANCH)
        self.assertEqual(raised.exception.status, 404)

    def test_a_finished_branch_build_has_no_manual_stage_left_to_drive(self):
        stages = [_normalize_stage(s) for s in self.api.pipeline_stage_info("275700446")]
        self.assertEqual(stages[-1]["name"], "发布")
        self.assertIsNone(_next_ready_manual_stage(stages))


if __name__ == "__main__":
    unittest.main()
