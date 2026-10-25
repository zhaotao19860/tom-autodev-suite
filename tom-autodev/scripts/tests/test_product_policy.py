import copy
import tempfile
import io
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline_plan import canonical_hash
from product_policy import target_for, select_product, compile_binding, ensure_compile
from stage_parameters import dependency_declaration
from state_store import StateStore
from clients.ipipe_client import IpipeApiClient


class ProductPolicyTests(unittest.TestCase):
    def target(self, changed=False):
        return {"run_id": "r", "module": "m", "branch": "b", "revision": "candidate" if changed else "published",
                "changed": changed, "pipeline_name": "ChangePipeline" if changed else "BranchPipeline",
                "plan_hash": "plan"}

    def fixture(self, changed=False):
        kind = "AGILE_COMPILE" if changed else "AGILE_RELEASE"
        revision = "candidate" if changed else "published"
        job = {"id": "j", "jobType": kind, "status": "SUCC", "realJobBuild": {
            "module": "m", "branch": "b", "revision": revision,
            "productHttpUrl": "https://example.test/compile",
            "releaseOutputHttpUrl": "https://example.test/release"}}
        build = {"id": "build", "module": "m", "branch": "b", "pipelineConfId": "p",
                 "revision": revision}
        stage = {"id": "s", "status": "SUCC", "jobBuildBeans": [job]}
        detail = {"entities": {"stageBuild": {"pipelineBuildId": "build", "pipelineConfId": "p",
                   "stageBuildId": "s", "status": "SUCC"}, "realJobBuilds": [[job]]}}
        api = SimpleNamespace(pipeline_stage_info=lambda _: [stage],
                              stage_detail=lambda _: detail,
                              verify_product_download=lambda *args: {"ok": True})
        return api, build, stage, detail, job

    def select(self, changed=False, mutate=None, owned=None):
        api, build, stage, detail, job = self.fixture(changed)
        if mutate:
            mutate(api, build, stage, detail, job)
        return select_product(api, self.target(changed), "p", [build],
                              owned_build_ids=owned or set())

    def test_frozen_changed_is_independent_of_worktree_clean(self):
        plan = {"run_id": "r", "modules": {"m": {"target_branch": "b",
                "expected_revisions": {"m": "candidate"}}}}
        plan["plan_hash"] = canonical_hash(plan)
        self.assertTrue(target_for(plan, "r", "m")["changed"])
        with self.assertRaisesRegex(ValueError, "PRODUCT_CHANGE_STATUS_UNKNOWN"):
            target_for(plan, "r", "unknown")
        plan["modules"]["m"]["target_branch"] = "other"
        with self.assertRaisesRegex(ValueError, "PIPELINE_PLAN_INVALID"):
            target_for(plan, "r", "m")

    def test_non_release_test_module_uses_explicit_frozen_status(self):
        plan = {"run_id": "r", "modules": {}, "product_inputs": {"qa": {
            "changed": True, "branch": "test", "revision": "sha", "pipeline_name": "ChangePipeline"}}}
        plan["plan_hash"] = canonical_hash(plan)
        self.assertTrue(target_for(plan, "r", "qa")["changed"])

    def test_baseline_release_returns_actual_published_revision(self):
        result = self.select()
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], "published")
        self.assertEqual(result["product_url"], "https://example.test/release")

    def test_baseline_release_must_match_frozen_revision_not_newest(self):
        api, newest, stage, detail, job = self.fixture()
        job["realJobBuild"].update(revision="newer-commit")
        newest.update(id="newest", revision="newer-commit")
        older = {**newest, "id": "older", "revision": "published"}
        old_job = {**job, "id": "j2", "realJobBuild": {**job["realJobBuild"], "revision": "published",
                   "releaseOutputHttpUrl": "https://example.test/old"}}
        def stage_info(build_id):
            return [{"id": "s-" + build_id, "status": "SUCC",
                     "jobBuildBeans": [job if build_id == "newest" else old_job]}]
        def stage_detail(stage_id):
            build_id = stage_id[2:]
            return {"entities": {"stageBuild": {"pipelineBuildId": build_id, "pipelineConfId": "p",
                    "stageBuildId": stage_id, "status": "SUCC"},
                    "realJobBuilds": [[job if build_id == "newest" else old_job]]}}
        api.pipeline_stage_info, api.stage_detail = stage_info, stage_detail
        result = select_product(api, self.target(), "p", [newest, older], owned_build_ids=set())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["revision"], "published")
        self.assertEqual(result["build_id"], "older")
        self.assertEqual(result["product_url"], "https://example.test/old")
        self.assertEqual(result["frozen_revision"], "published")

    def two_exact_candidates(self, downloads):
        """Two successful releases of exactly the frozen revision, newest first."""
        api, newest, stage, detail, job = self.fixture()
        older = {**newest, "id": "older"}
        jobs = {build_id: {**job, "id": "j-" + build_id, "realJobBuild": {
            **job["realJobBuild"], "releaseOutputHttpUrl": f"https://example.test/{build_id}"}}
            for build_id in ("build", "older")}
        api.pipeline_stage_info = lambda build_id: [
            {"id": "s-" + build_id, "status": "SUCC", "jobBuildBeans": [jobs[build_id]]}]
        api.stage_detail = lambda stage_id: {"entities": {"stageBuild": {
            "pipelineBuildId": stage_id[2:], "pipelineConfId": "p", "stageBuildId": stage_id,
            "status": "SUCC"}, "realJobBuilds": [[jobs[stage_id[2:]]]]}}
        checked = []

        def verify(url, module):
            checked.append(url.rsplit("/", 1)[-1])
            return dict(downloads[url.rsplit("/", 1)[-1]])

        api.verify_product_download = verify
        return select_product(api, self.target(), "p", [newest, older], owned_build_ids=set()), checked

    def test_a_failed_download_moves_on_to_the_next_exact_candidate(self):
        result, checked = self.two_exact_candidates({
            "build": {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_status": 404},
            "older": {"ok": True},
        })

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["build_id"], "older")
        self.assertEqual(result["product_url"], "https://example.test/older")
        self.assertEqual(checked, ["build", "older"])

    def test_only_when_every_exact_candidate_fails_is_the_failure_returned(self):
        result, checked = self.two_exact_candidates({
            "build": {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_status": 404},
            "older": {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "http_status": 503},
        })

        self.assertEqual(result["reason_code"], "PRODUCT_DOWNLOAD_UNVERIFIED", result)
        self.assertEqual(checked, ["build", "older"])
        self.assertEqual(result["http_statuses"], [404, 503])
        self.assertEqual([item["build_id"] for item in result["candidates"]], ["build", "older"])
        self.assertNotIn("https://", str(result))

    def test_baseline_without_matching_revision_fails_and_lists_seen_prefixes(self):
        result = self.select(mutate=lambda a,b,s,d,j: (
            j["realJobBuild"].update(revision="0f0d51604d4fec269111f17ef9a14c8c8d62bed5"),
            b.update(revision="0f0d51604d4fec269111f17ef9a14c8c8d62bed5")))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "PRODUCT_REVISION_MISMATCH")
        self.assertEqual(result["seen_revisions"], ["0f0d5160"])
        self.assertNotIn("product_url", result)

    def test_baseline_scan_is_bounded_and_skips_failed_builds(self):
        from product_policy import RELEASE_SCAN_LIMIT
        api, build, stage, detail, job = self.fixture()
        calls = []
        api.pipeline_stage_info = lambda build_id: calls.append(build_id) or []
        failed = {**build, "id": "failed", "status": "FAIL"}
        many = [{**build, "id": f"b{i}"} for i in range(RELEASE_SCAN_LIMIT + 10)]
        result = select_product(api, self.target(), "p", [failed] + many, owned_build_ids=set())
        self.assertEqual(result["reason_code"], "PRODUCT_URL_NOT_PUBLISHED")
        self.assertEqual(len(calls), RELEASE_SCAN_LIMIT)
        self.assertNotIn("failed", calls)

    def test_baseline_never_reuses_arbitrary_compile(self):
        self.assertFalse(self.select(mutate=lambda a,b,s,d,j:
            j.update(jobType="AGILE_COMPILE"))["ok"])

    def test_changed_requires_owned_exact_candidate(self):
        self.assertFalse(self.select(True)["ok"])
        self.assertTrue(self.select(True, owned={"build"})["ok"])
        self.assertFalse(self.select(True, owned={"build"}, mutate=lambda a,b,s,d,j:
            j["realJobBuild"].update(revision="old"))["ok"])

    def test_wrong_branch_pipeline_identity_and_missing_revision_are_rejected(self):
        for mutate in (
            lambda a,b,s,d,j: b.update(branch="other"),
            lambda a,b,s,d,j: b.update(pipelineConfId="other"),
            lambda a,b,s,d,j: j["realJobBuild"].pop("revision"),
            lambda a,b,s,d,j: d["entities"]["stageBuild"].update(pipelineBuildId="other"),
            lambda a,b,s,d,j: j.update(status="FAIL"),
        ):
            self.assertFalse(self.select(mutate=mutate)["ok"])

    def test_url_presence_is_not_download_proof(self):
        result = self.select(mutate=lambda a,b,s,d,j:
            setattr(a, "verify_product_download", lambda *args: {"ok": False}))
        self.assertEqual(result["reason_code"], "PRODUCT_DOWNLOAD_UNVERIFIED")

    def test_actual_adapter_does_not_invent_capabilities(self):
        client = IpipeApiClient(SimpleNamespace(), current_user="user")
        self.assertFalse(client.compile_only_capability()["supported"])
        self.assertFalse(client.stage_job_override_capability()["supported"])
        with self.assertRaisesRegex(ValueError, "COMPILE_ONLY_TRIGGER_UNSUPPORTED"):
            compile_binding(self.target(), "p", client.compile_only_capability())

    def test_compile_timeout_restart_only_reconciles(self):
        capability = {"supported": True, "release_excluded": True,
                      "evidence_ref": "fake-contract", "contract_hash": "hash"}
        calls = []
        def trigger(binding, intent):
            calls.append((binding, intent))
            raise TimeoutError()
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.db")
            runtime = SimpleNamespace(run_id="r", state=state, approvals=object(),
                _profile_error=lambda: None, api=SimpleNamespace(
                    compile_only_capability=lambda: capability,
                    reconcile_branch_compile=lambda *args: None,
                    trigger_branch_compile=trigger))
            approval = {"approval_id": "a"}
            with patch("clients.ipipe_runtime._approved_record", return_value=None):
                first = ensure_compile(runtime, self.target(), "p", approval)
                second = ensure_compile(runtime, self.target(), "p", approval)
                self.assertEqual(first["reason_code"], "BRANCH_COMPILE_RESULT_UNKNOWN")
                self.assertEqual(second["reason_code"], "QUERY_REQUIRED")
                binding, intent = calls[0]
                runtime.api.reconcile_branch_compile = lambda *args: {
                    **{k:binding[k] for k in (
                        "module", "branch", "revision", "pipeline_id", "scope", "release_allowed")},
                    "build_id": "build", "binding_hash": canonical_hash(binding), "intent_id": intent}
                self.assertTrue(ensure_compile(runtime, self.target(), "p", approval)["ok"])
                self.assertTrue(ensure_compile(runtime, self.target(), "p", approval)["ok"])
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0][0]["release_allowed"])

    def test_empty_manual_params_still_discovers_inherited_get_bgwagent(self):
        stage = {"stageConfId": 2, "stageName": "P0", "manualParams": None, "hasDynamicParams": False}
        conf = {"stages": [
            {"stageConfId": 1, "paramBeans": [{"name": "get_bgwagent", "defaultValue": "node97110098"}]},
            {"stageConfId": 2, "stageName": "P0", "upstreamStageId": 1,
             "paramBeans": [{"name": "get_bgw_test_case"}],
             "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
                 "command": 'eval "${get_bgwagent}"'}}}]]}]}
        detail = {"realJobBuilds": [[{"jobConfId": 3}]]}
        result = dependency_declaration(stage, detail, conf)
        self.assertEqual(result["names"], ["get_bgw_test_case", "get_bgwagent"])
        self.assertTrue(result["requires_job_override"])
        self.assertNotIn("97110098", str(result))
        del conf["stages"][0]["paramBeans"]
        self.assertEqual(dependency_declaration(stage, detail, conf)["reason_code"],
                         "STAGE_DEPENDENCY_INPUT_UNDECLARED")

    def test_real_shape_p0_blocks_before_g8_with_inherited_names(self):
        from clients.ipipe_runtime import IpipeRuntime
        runtime = IpipeRuntime.__new__(IpipeRuntime)
        runtime._load_stage_binding = lambda _: {
            "build_id": "b", "context": {"pipeline_id": "p"},
            "stage": {"stage_conf_id": "2"}}
        stage = {"stageBuildId": "s", "pipelineBuildId": "b", "pipelineConfId": "p",
                 "stageConfId": 2, "stageName": "P0", "manualParams": None,
                 "hasDynamicParams": False}
        configuration = {"id": "p", "stages": [
            {"stageConfId": 1, "paramBeans": [{"name": "get_bgwagent",
                                             "defaultValue": "secret node97110098"}]},
            {"stageConfId": 2, "stageName": "P0", "upstreamStageId": 1,
             "paramBeans": [{"name": "get_bgw_test_case"}],
             "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
                 "command": 'eval "${get_bgwagent}"'}}}]]}]}
        runtime.api = SimpleNamespace(
            get_pipeline_by_id=lambda *args, **kwargs: configuration,
            stage_detail=lambda _: {"entities": {
                "stageBuild": stage, "realJobBuilds": [[{"jobConfId": 3}]]}},
            stage_job_override_capability=lambda: {"supported": False})
        result = runtime.stage_parameter_names("s")
        self.assertEqual(result["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")
        self.assertEqual(result["names"], ["get_bgw_test_case", "get_bgwagent"])
        self.assertNotIn("secret", str(result))
        # A capability claim does not create a write: manual_execute_stage cannot carry an
        # inherited name, and `_rerun_binding` refuses it, so material preparation does too.
        runtime.api.stage_job_override_capability = lambda: {
            "supported": True, "evidence_ref": "fake", "contract_hash": "hash"}
        again = runtime.stage_parameter_names("s")
        self.assertEqual(again["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")
        self.assertEqual(again["parameters"], ["get_bgwagent"])

    def _p0_stage_fixture(self, stage_name, *, inherited=True):
        """A stage identical in shape to test_real_shape_p0_blocks_before_g8_with_inherited_names,
        but parameterized on stage_name so both P0新case回归 (historical) and
        p0级自动化回归测试 (current BGW P0 automation) can be exercised identically.
        `inherited=False` declares get_bgwagent on the stage itself, the executable shape."""
        stage = {"stageBuildId": "s", "pipelineBuildId": "b", "pipelineConfId": "p",
                 "stageConfId": 2, "stageName": stage_name, "manualParams": None,
                 "hasDynamicParams": False}
        if not inherited:
            return stage, {"id": "p", "stages": [
                {"stageConfId": 2, "stageName": stage_name,
                 "paramBeans": [{"name": "get_bgw_test_case"}, {"name": "get_bgwagent"}],
                 "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
                     "command": 'eval "${get_bgwagent}"'}}}]]}]}
        configuration = {"id": "p", "stages": [
            {"stageConfId": 1, "paramBeans": [{"name": "get_bgwagent",
                                             "defaultValue": "secret node97110098"}]},
            {"stageConfId": 2, "stageName": stage_name, "upstreamStageId": 1,
             "paramBeans": [{"name": "get_bgw_test_case"}],
             "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
                 "command": 'eval "${get_bgwagent}"'}}}]]}]}
        return stage, configuration

    def _p0_runtime(self, stage_name, *, validated_profile, product_proofs=None, inherited=True):
        from clients.ipipe_runtime import IpipeRuntime
        stage, configuration = self._p0_stage_fixture(stage_name, inherited=inherited)
        runtime = IpipeRuntime.__new__(IpipeRuntime)
        runtime._load_stage_binding = lambda _: {
            "build_id": "b", "context": {"pipeline_id": "p"},
            "stage": {"stage_conf_id": "2"}}
        runtime.validated_profile = validated_profile
        runtime._product_proofs = product_proofs or {}
        runtime.run_id = "run-p0"
        # Every real runtime has a state store; this one knows of no superseded build.
        runtime.state = SimpleNamespace(idempotency_result=lambda key: None)
        runtime.api = SimpleNamespace(
            get_pipeline_by_id=lambda *args, **kwargs: configuration,
            stage_detail=lambda _: {"entities": {
                "stageBuild": stage, "realJobBuilds": [[{"jobConfId": 3}]]}},
            # The real adapter always reports unsupported; the whitelist path must still
            # allow a known, product-verified name through without ever calling this.
            stage_job_override_capability=lambda: {"supported": False})
        return runtime

    def test_allowlisted_inherited_parameter_is_refused_when_material_is_prepared(self):
        # (a) get_bgwagent is recorded project knowledge and has a verified product, but it
        # is inherited from stage 1, so only a job-level override could carry it and none
        # exists. The declaration used to pass here (allowlist bypass) while
        # `_rerun_binding` refused the same stage at hash time; one shared decision
        # (`job_override_decision`) now refuses it while the G8 material is prepared.
        for stage_name in ("P0新case回归", "p0级自动化回归测试"):
            with self.subTest(stage_name=stage_name):
                runtime = self._p0_runtime(
                    stage_name,
                    validated_profile={"project_id": "bgw"},
                    product_proofs={"baidu/sysip/bgwagent": {
                        "download_verified": True, "product_url": "https://irepo.example/x"}},
                )
                result = runtime.stage_parameter_names("s")
                bound = runtime._rerun_binding("s", {
                    "get_bgw_test_case": "x", "get_bgwagent": "y"})
                self.assertEqual(result["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")
                self.assertEqual(result["parameters"], ["get_bgwagent"])
                self.assertEqual(result["allowlisted_parameters"], ["get_bgwagent"])
                self.assertEqual(bound["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")

    def test_parameters_declared_on_the_stage_itself_stay_executable(self):
        runtime = self._p0_runtime(
            "p0级自动化回归测试", validated_profile={"project_id": "bgw"}, inherited=False)
        result = runtime.stage_parameter_names("s")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["names"], ["get_bgw_test_case", "get_bgwagent"])

    def test_known_whitelisted_parameter_without_verified_product_still_blocks(self):
        # (b) the same known-whitelisted name, but with no product proof recorded yet =>
        # the stage_job_override_capability block is still bypassed here (declaration
        # succeeds), and it is _rerun_binding's existing PRODUCT_PROOF_REQUIRED check --
        # not a new reason code -- that blocks assembly once a value is actually supplied.
        # This proves the narrowing never grants a free pass: it only lets a whitelisted
        # name past the override gate so the ordinary product-proof gate can still refuse it.
        from clients.ipipe_runtime import IpipeRuntime
        runtime = self._p0_runtime(
            "p0级自动化回归测试", validated_profile={"project_id": "bgw"}, product_proofs={},
            inherited=False)
        declaration = runtime.stage_parameter_names("s")
        self.assertTrue(declaration["ok"])
        bound = runtime._rerun_binding("s", {
            "get_bgw_test_case": "x", "get_bgwagent": "y"})
        self.assertEqual(bound["reason_code"], "PRODUCT_PROOF_REQUIRED")

    def test_proof_revision_must_equal_the_binding_repository_revision(self):
        from stage_parameters import product_command
        module = "baidu/sysip/bgwagent"
        url = "https://irepo.example/x"
        runtime = self._p0_runtime(
            "p0级自动化回归测试", validated_profile={"project_id": "bgw"},
            product_proofs={module: {
                "download_verified": True, "product_url": url,
                "revision": "0f0d51604d4f", "frozen_revision": "97860aca1dc9"}},
            inherited=False)
        runtime._load_stage_binding = lambda _: {
            "build_id": "b", "stage": {"stage_conf_id": "2", "status": "FAIL"},
            "context": {"pipeline_id": "p", "repositories": [
                {"module": module, "revision": "97860aca1dc9"}]}}
        runtime._product_proofs["baidu/nsiqa/x86bgw"] = {
            "download_verified": True, "product_url": "https://irepo.example/t",
            "revision": "aaaa", "frozen_revision": "aaaa"}
        bound = runtime._rerun_binding("s", {
            "get_bgw_test_case": product_command("https://irepo.example/t", "<IREPO-TOKEN>"),
            "get_bgwagent": product_command(url, "<IREPO-TOKEN>")})
        self.assertEqual(bound["reason_code"], "PRODUCT_PARAMETER_BINDING_MISMATCH")
        self.assertEqual(bound["parameter"], "get_bgwagent")
        self.assertEqual(bound["reason"], "PRODUCT_REVISION_MISMATCH")

    def test_unknown_job_variable_still_blocks_regardless_of_product_proofs(self):
        # (c) a job shell variable that is declared (so dependency_declaration can resolve
        # it) but is NOT in project knowledge (no _PARAMETER_ALIASES entry, no profile
        # stage_parameter_mapping) must keep blocking with JOB_PARAMETER_OVERRIDE_UNSUPPORTED
        # even if some unrelated module happens to have a verified product proof recorded.
        # The narrowing must never become "any job variable with any proof passes" -- it
        # only recognizes names the project already knows are legitimate BGW form fields.
        from clients.ipipe_runtime import IpipeRuntime
        stage = {"stageBuildId": "s", "pipelineBuildId": "b", "pipelineConfId": "p",
                 "stageConfId": 2, "stageName": "p0级自动化回归测试", "manualParams": None,
                 "hasDynamicParams": False}
        configuration = {"id": "p", "stages": [
            {"stageConfId": 2, "stageName": "p0级自动化回归测试",
             "paramBeans": [{"name": "get_totally_unknown_variable"}],
             "groupJobs": [[{"jobConfId": 3, "realJob": {"shellConf": {
                 "command": 'eval "${get_totally_unknown_variable}"'}}}]]}]}
        runtime = IpipeRuntime.__new__(IpipeRuntime)
        runtime._load_stage_binding = lambda _: {
            "build_id": "b", "context": {"pipeline_id": "p"},
            "stage": {"stage_conf_id": "2"}}
        runtime.validated_profile = {"project_id": "bgw"}
        runtime._product_proofs = {"baidu/sysip/bgwagent": {
            "download_verified": True, "product_url": "https://irepo.example/x"}}
        runtime.api = SimpleNamespace(
            get_pipeline_by_id=lambda *args, **kwargs: configuration,
            stage_detail=lambda _: {"entities": {
                "stageBuild": stage, "realJobBuilds": [[{"jobConfId": 3}]]}},
            stage_job_override_capability=lambda: {"supported": False})
        result = runtime.stage_parameter_names("s")
        self.assertEqual(result["reason_code"], "JOB_PARAMETER_OVERRIDE_UNSUPPORTED")

    def test_download_redirect_never_forwards_credential_or_saves_location(self):
        client = IpipeApiClient(SimpleNamespace(timeout_seconds=1), current_user="user")
        url = "https://irepo.baidu-int.com/rest/prod/v3/m/releases/1/files"
        calls = []
        class Response:
            status = 206
            def read(self, size):
                return b"\x1f\x8b"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        def open_request(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 307, "redirect", {
                    "Location": "http://product.test/file?signature=private"}, io.BytesIO())
            return Response()
        with patch("stage_parameters.load_tokens", return_value={"m": "opaque-secret"}), \
                patch("urllib.request.build_opener", return_value=SimpleNamespace(open=open_request)):
            result = client.verify_product_download(url, "m")
        self.assertTrue(result["ok"])
        self.assertIn("opaque-secret", str(calls[0].headers))
        self.assertNotIn("opaque-secret", str(calls[1].headers))
        self.assertNotIn("signature", str(result))

    def test_runtime_uses_frozen_branch_release_and_blocks_missing_compile_contract(self):
        from clients.ipipe_runtime import IpipeRuntime
        api, build, stage, detail, job = self.fixture()
        api.pipelines_by_module = lambda _: [{"id": "p", "moduleName": "m",
                                             "pipelineName": "BranchPipeline"}]
        api.branch_product_builds = lambda *args: [build]
        api.compile_only_capability = lambda: {"supported": False}
        runtime = IpipeRuntime.__new__(IpipeRuntime)
        runtime.run_id = "r"
        runtime.api = api
        runtime.state = SimpleNamespace(events=lambda _: [])
        plan = {"run_id": "r", "modules": {}, "unchanged": {"m": {
            "module": "m", "branch": "b", "revision": "published"}}}
        plan["plan_hash"] = canonical_hash(plan)
        with patch("pipeline_plan.frozen_plan", return_value=plan):
            found = runtime.product_for_plan(plan, "m")
            self.assertTrue(found["ok"])
            self.assertEqual(found["frozen_revision"], "published")
            # The branch moved on: the published release is a different revision, so it is
            # refused (never substituted) and, without a compile contract, blocked.
            moved = copy.deepcopy(plan)
            moved["unchanged"]["m"]["revision"] = "frozen-older"
            moved.pop("plan_hash")
            moved["plan_hash"] = canonical_hash(moved)
            with patch("pipeline_plan.frozen_plan", return_value=moved):
                mismatch = runtime.product_for_plan(moved, "m")
            self.assertEqual(mismatch["reason_code"], "PRODUCT_REVISION_MISMATCH")
            self.assertEqual(mismatch["seen_revisions"], ["publishe"])
            api.branch_product_builds = lambda *args: []
            self.assertEqual(runtime.product_for_plan(plan, "m")["reason_code"],
                             "COMPILE_ONLY_TRIGGER_UNSUPPORTED")
        with patch("pipeline_plan.frozen_plan", return_value=None):
            self.assertEqual(runtime.product_for_plan(plan, "m")["reason_code"],
                             "PIPELINE_PLAN_MISMATCH")

    def test_worker_compile_approval_binding_is_persisted_without_a_trigger(self):
        import worker_driver
        target = self.target()
        capability = {"supported": True, "release_excluded": True,
                      "evidence_ref": "fake-contract", "contract_hash": "hash"}
        binding = compile_binding(target, "p", capability)
        digest = canonical_hash(binding)
        saved = {}
        state = SimpleNamespace(save_idempotency_result=lambda k,v: saved.setdefault(k,v))
        orchestrator = SimpleNamespace(state=state)
        resolved = {"ok": False, "reason_code": "BRANCH_COMPILE_APPROVAL_REQUIRED",
                    "binding": binding, "input_hash": digest}
        with patch("orchestrator._stage_parameters", return_value=resolved), \
                patch.object(worker_driver, "_settled_approval_id", return_value=None):
            result = worker_driver._prepare_manual_stage(
                orchestrator, object(), "r", "s", "m")
        self.assertEqual(result["approval_input_hash"], digest)
        self.assertEqual(result["gate"], "G8")
        self.assertEqual(saved[f"ipipe.g8-material:r:{digest}"]["binding"], binding)


if __name__ == "__main__":
    unittest.main()
