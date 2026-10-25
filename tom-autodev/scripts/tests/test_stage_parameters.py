import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cli_transport
import orchestrator as orchestrator_module
import stage_parameters

URL = "https://irepo.baidu-int.com/rest/prod/v3/baidu/sysip/bgwagent/nodes/102009429/files"
TOKEN = "82f7b234-8b56-40c5-9011-5492673ed8d8"


class StageParameterTests(unittest.TestCase):
    def test_explicit_empty_and_unknown_declarations_are_distinct(self):
        self.assertEqual(stage_parameters.declared_names(
            {"manualParams": None, "hasDynamicParams": False})["names"], [])
        for declaration in ({}, {"manualParams": None}, {"manualParams": [], "hasDynamicParams": True},
                            {"manualParams": [{"unexpected": "x"}]}):
            self.assertFalse(stage_parameters.declared_names(declaration)["ok"])
        self.assertEqual(stage_parameters.declared_names(
            {"manualParams": '[{"name":"get_bgwagent"}]'})["names"], ["get_bgwagent"])

    def test_mapping_is_exact_and_all_unknown_inputs_are_reported(self):
        result = stage_parameters.resolve(
            ["x86bgw_product", "prefix_get_bgwagent", "random_cr_id"],
            change_number="1", product_urls={"baidu/sysip/x86bgw": URL}, tokens={},
        )
        self.assertEqual(len(result["missing_parameters"]), 3)
        self.assertEqual(stage_parameters.resolve(
            [], change_number=None, product_urls={}, tokens={})["parameters"], {})
        bgw = {"project_id": "bgw"}
        # p0级自动化回归测试 is the current BGW P0 automation stage name and carries the same
        # known get_bgw_test_case/get_bgwagent form fields as the historical P0新case回归 stage.
        self.assertEqual(stage_parameters.project_mapping(
            bgw, "p0级自动化回归测试")["get_bgwagent"], "baidu/sysip/bgwagent")
        self.assertEqual(stage_parameters.project_mapping(
            bgw, "P0新case回归")["get_bgw_test_case"], "baidu/nsiqa/x86bgw")
        self.assertEqual(stage_parameters.project_mapping(
            {"project_id": "other"}, "P0新case回归"), {})
        self.assertEqual(stage_parameters.project_mapping(bgw, "unrelated_stage"), {})
        profile = {"pipeline_profile": {"stage_parameter_mapping": {
            "manual": {"input": "baidu/test/product"}}}}
        self.assertEqual(stage_parameters.project_mapping(profile, "manual"),
                         {"input": "baidu/test/product"})

    def test_both_parameters_of_the_bgw_new_case_stage_are_derived(self):
        qa_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/nsiqa/x86bgw/nodes/102407103/files"
        qa_token = "50c9c669-7fbf-442f-b61a-51d2c36ef172"
        resolved = stage_parameters.resolve(
            ["get_bgw_test_case", "get_bgwagent"],
            change_number="122527554",
            product_urls={
                "baidu/nsiqa/x86bgw": qa_url,
                "baidu/sysip/bgwagent": URL,
            },
            tokens={
                "baidu/nsiqa/x86bgw": qa_token,
                "baidu/sysip/bgwagent": TOKEN,
            },
        )

        self.assertTrue(resolved["ok"])
        self.assertIn(f'--header "IREPO-TOKEN:{qa_token}"', resolved["parameters"]["get_bgw_test_case"])
        self.assertIn(qa_url, resolved["parameters"]["get_bgw_test_case"])
        self.assertIn(f'--header "IREPO-TOKEN:{TOKEN}"', resolved["parameters"]["get_bgwagent"])
        self.assertIn(URL, resolved["parameters"]["get_bgwagent"])

    def test_get_bgw_test_case_does_not_bind_the_business_x86bgw_module(self):
        qa_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/nsiqa/x86bgw/nodes/102407103/files"
        business_url = "https://irepo.baidu-int.com/rest/prod/v3/baidu/sysip/x86bgw/nodes/102009480/files"
        resolved = stage_parameters.resolve(
            ["get_bgw_test_case"],
            change_number="122527554",
            product_urls={
                "baidu/sysip/x86bgw": business_url,
                "baidu/nsiqa/x86bgw": qa_url,
            },
            tokens={"baidu/nsiqa/x86bgw": "50c9c669-7fbf-442f-b61a-51d2c36ef172"},
        )

        self.assertTrue(resolved["ok"])
        self.assertIn(qa_url, resolved["parameters"]["get_bgw_test_case"])
        self.assertNotIn(business_url, resolved["parameters"]["get_bgw_test_case"])

    def test_a_missing_input_names_the_parameter_instead_of_guessing(self):
        no_cr = stage_parameters.resolve(
            ["test_cr_id"], change_number=None, product_urls={}, tokens={}
        )
        no_token = stage_parameters.resolve(
            ["get_bgwagent"],
            change_number="1",
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={},
        )
        unknown = stage_parameters.resolve(
            ["some_other_input"], change_number="1", product_urls={}, tokens={}
        )

        for result, detail in (
            (no_cr, "SUBMISSION_RECEIPT_REQUIRED"),
            (no_token, "IREPO_TOKEN_REQUIRED"),
            (unknown, "PARAMETER_NOT_DERIVABLE"),
        ):
            self.assertEqual(result["reason_code"], "STAGE_PARAMETER_UNRESOLVED")
            self.assertEqual(result["detail"], detail)

    def test_evidence_never_carries_the_token(self):
        values = stage_parameters.resolve(
            ["test_cr_id", "get_bgwagent"],
            change_number="122402145",
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={"baidu/sysip/bgwagent": TOKEN},
        )["parameters"]

        safe = stage_parameters.redacted(values)

        self.assertNotIn(TOKEN, str(safe))
        self.assertIn("<IREPO-TOKEN>", safe["get_bgwagent"])
        self.assertEqual(safe["test_cr_id"], "122402145")

    def test_evidence_redacts_a_non_uuid_token(self):
        # load_tokens accepts any non-empty string, so redaction must not depend on the
        # token being UUID-shaped: a hex blob / JWT / opaque token must still be scrubbed.
        opaque = "gho_ABCdef0123456789ghIJKLmnopQRSTuvwx4242"
        values = stage_parameters.resolve(
            ["get_bgwagent"],
            change_number=None,
            product_urls={"baidu/sysip/bgwagent": URL},
            tokens={"baidu/sysip/bgwagent": opaque},
        )["parameters"]

        safe = stage_parameters.redacted(values)

        self.assertNotIn(opaque, str(safe))
        self.assertIn('--header "IREPO-TOKEN:<IREPO-TOKEN>"', safe["get_bgwagent"])

    def test_a_world_readable_token_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "credentials").mkdir()
            path = root / "credentials" / "irepo-tokens.yaml"
            path.write_text("baidu/sysip/bgwagent: %s\n" % TOKEN, encoding="utf-8")
            os.chmod(path, 0o600)
            tight = stage_parameters.load_tokens(root)
            os.chmod(path, 0o644)

            with self.assertRaises(ValueError) as refused:
                stage_parameters.load_tokens(root)

        self.assertEqual(tight, {"baidu/sysip/bgwagent": TOKEN})
        self.assertEqual(str(refused.exception), "IREPO_TOKEN_FILE_PERMISSIONS")

    def test_no_configured_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(stage_parameters.load_tokens(Path(directory)), {})


class ManualStageControlPlaneTests(unittest.TestCase):
    def runtime(self, declaration):
        from clients.ipipe_runtime import IpipeRuntime
        runtime = IpipeRuntime.__new__(IpipeRuntime)
        owned = {"build_id": "build", "context": {"pipeline_id": "pipeline"},
                 "stage": {"stage_conf_id": "conf"}}
        runtime._load_stage_binding = lambda stage_id: owned if stage_id == "stage" else None
        runtime.api = SimpleNamespace(stage_detail=lambda stage_id: {"entities": {
            "stageBuild": {"stageBuildId": stage_id, "pipelineBuildId": "build",
                           "pipelineConfId": "pipeline", "stageConfId": "conf",
                           "stageName": "manual", **declaration}}})
        return runtime

    def test_discovery_requires_owned_matching_platform_identity(self):
        runtime = self.runtime({"manualParams": None, "hasDynamicParams": False})
        self.assertEqual(runtime.stage_parameter_names("stage")["names"], [])
        self.assertEqual(runtime.stage_parameter_names("other")["reason_code"],
                         "STAGE_OWNERSHIP_UNVERIFIED")
        runtime = self.runtime({"manualParams": [], "pipelineBuildId": "other"})
        self.assertEqual(runtime.stage_parameter_names("stage")["reason_code"],
                         "STAGE_PARAMETER_IDENTITY_MISMATCH")

    def test_cli_resolution_without_names_reads_declaration(self):
        runtime = self.runtime({"manualParams": None, "hasDynamicParams": False})
        result = orchestrator_module._stage_parameters(None, runtime, "run", [], "stage")
        self.assertEqual(result["parameters"], {})
        self.assertIn("manualParams", result["declaration"]["source"])
        self.assertFalse(orchestrator_module._stage_parameters(None, runtime, "run", [])["ok"])
        mismatch = orchestrator_module._stage_parameters(
            None, runtime, "run", ["get_bgwagent"], "stage")
        self.assertEqual(mismatch["reason_code"], "STAGE_PARAMETER_NAMES_MISMATCH")

    def test_unknown_mapping_fails_before_products_or_credentials(self):
        runtime = self.runtime({"manualParams": ["first", "second"]})
        orchestrator = SimpleNamespace(_runtime_profile=lambda run: {
            "ok": True, "profile": {"project_id": "bgw"}})
        result = orchestrator_module._stage_parameters(orchestrator, runtime, "run", [], "stage")
        self.assertEqual([p["parameter"] for p in result["missing_parameters"]], ["first", "second"])

    def test_discovered_profile_mapping_uses_exact_pinned_products(self):
        runtime = self.runtime({"manualParams": ["input"]})
        product_calls = []
        runtime.product_for_plan = lambda plan, module: (
            product_calls.append((module, "agent-revision")) or {
                "ok": True, "product_url": URL, "revision": "agent-revision"})
        profile = {"project_id": "other", "pipeline_profile": {
            "stage_parameter_mapping": {"manual": {"input": "baidu/sysip/bgwagent"}}}}
        orchestrator = SimpleNamespace(
            _runtime_profile=lambda run: {"ok": True, "profile": profile},
            artifacts=SimpleNamespace(artifacts_for_run=lambda run: []),
            config_root=Path("/unused"),
            state=SimpleNamespace(events=lambda run: []),
        )
        revisions = {"ok": True, "revisions": {"repositories": [
            {"module": "baidu/sysip/bgwagent", "revision": "agent-revision"},
            {"module": "baidu/sysip/x86bgw", "revision": "business-revision"},
        ]}}
        with patch("pipeline_plan.frozen_plan", return_value={"plan_hash": "frozen"}), \
                patch.object(orchestrator_module, "_ipipe_revision_set", return_value=revisions), \
                patch.object(stage_parameters, "load_tokens", return_value={"baidu/sysip/bgwagent": TOKEN}):
            result = orchestrator_module._stage_parameters(orchestrator, runtime, "run", [], "stage")
        self.assertTrue(result["ok"])
        self.assertEqual(product_calls, [("baidu/sysip/bgwagent", "agent-revision")])
        self.assertIn(TOKEN, result["parameters"]["input"])

    def resolve_gone_bgwagent(self, **options):
        """Resolve a P0 stage whose `get_bgwagent` answers 404 on every candidate."""
        from state_store import StateStore
        runtime = self.runtime({"manualParams": ["get_bgwagent"]})
        gone = {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED", "module": "baidu/sysip/bgwagent",
                "build_id": "agent-new", "http_statuses": [404],
                "candidates": [{"build_id": "agent-new", "http_status": 404},
                               {"build_id": "agent-old", "http_status": 404}],
                "download_check": {"build_id": "agent-old", "http_status": 404}}
        runtime.product_for_plan = lambda plan, module: dict(gone)
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        state = StateStore(Path(root.name) / "state.sqlite")
        orchestrator = SimpleNamespace(
            _runtime_profile=lambda run: {"ok": True, "profile": {"project_id": "bgw"}},
            artifacts=SimpleNamespace(artifacts_for_run=lambda run: []), state=state,
        )
        revisions = {"ok": True, "revisions": {"repositories": [
            {"module": "baidu/sysip/bgwagent", "revision": "agent-revision"}]}}
        with patch("pipeline_plan.frozen_plan", return_value={"plan_hash": "frozen"}), \
                patch.object(orchestrator_module, "_ipipe_revision_set", return_value=revisions), \
                patch("stage_parameters.project_mapping",
                      return_value={"get_bgwagent": "baidu/sysip/bgwagent"}):
            runtime.stage_parameter_names = lambda stage: {
                "ok": True, "names": ["get_bgwagent"], "stage_name": "P0新case回归"}
            result = orchestrator_module._stage_parameters(
                orchestrator, runtime, "run", [], "stage", **options)
        return result, state

    def test_b11_get_bgwagent_gone_on_every_candidate_is_recorded_for_the_stage(self):
        result, state = self.resolve_gone_bgwagent(record_evidence=True)

        self.assertEqual(result["reason_code"], "PRODUCT_DOWNLOAD_UNVERIFIED")
        # Keyed by the build and stage the parameters belong to, not the product's build.
        recorded = state.idempotency_result(
            "ipipe.dependency-failure:run:build:stage:baidu/sysip/bgwagent:0001")
        self.assertEqual(
            (recorded["classification"], recorded["http_statuses"], recorded["stage_conf_id"],
             recorded["consumed_by"], recorded["declared_parameters"]),
            ("ENV_UNSATISFIED", [404], "conf", ["get_bgwagent"], ["get_bgwagent"]))

    def test_b21_read_only_resolution_writes_no_dependency_evidence(self):
        result, state = self.resolve_gone_bgwagent()

        self.assertEqual(result["reason_code"], "PRODUCT_DOWNLOAD_UNVERIFIED")
        self.assertEqual(state.idempotency_results_with_prefix("ipipe.dependency-failure:"), [])

    def test_b21_cli_print_parameters_and_input_hash_write_no_dependency_evidence(self):
        from types import SimpleNamespace as Namespace
        seen = []

        def fake_stage_parameters(*args, **kwargs):
            seen.append(kwargs.get("record_evidence"))
            return {"ok": False, "reason_code": "PRODUCT_DOWNLOAD_UNVERIFIED"}

        runtime = Namespace(rerun=lambda *a, **k: {"ok": False})
        with tempfile.TemporaryDirectory() as root, \
                patch("orchestrator.Orchestrator"), \
                patch("orchestrator._cli_ipipe_runtime", return_value=runtime), \
                patch("orchestrator._stage_parameters", side_effect=fake_stage_parameters), \
                contextlib.redirect_stdout(io.StringIO()):
            for flag in ("--print-parameters", "--print-input-hash", "--request-g8"):
                orchestrator_module.main(["--config-root", root, "ipipe-rerun", "run",
                                          "stage", "-", "-", flag])
        self.assertEqual(seen, [False, False, True])

    def test_manual_monitor_enters_g8_preparation_in_worker_controller(self):
        import worker_driver
        runtime = SimpleNamespace(
            trigger_input_hash=lambda *args: {"ok": True, "input_hash": "trigger"},
            discover=lambda *args: {"ok": True, "build_id": "build"},
            monitor_once=lambda *args: {"status": "MANUAL_WAIT", "stage_build_id": "stage"},
        )
        checkpoints = []
        orchestrator = SimpleNamespace(
            _runtime_profile=lambda run: {"ok": True, "profile": {}},
            ipipe_runtime=lambda *args: runtime,
            state=SimpleNamespace(save_ipipe_monitoring=lambda *args: checkpoints.append(args)),
        )
        with patch.object(worker_driver, "_next_ipipe_module", return_value="module"), \
                patch.object(worker_driver, "_ipipe_module_binding", return_value={"bound": True}), \
                patch.object(orchestrator_module, "_ipipe_revision_set",
                             return_value={"ok": True, "revisions": {}}), \
                patch.object(worker_driver, "_settled_approval_id", return_value=None), \
                patch.object(worker_driver, "_prepare_manual_stage",
                             return_value={"gate": "G8"}) as prepare:
            result = worker_driver._execute_ipipe(orchestrator, "run", {}, object())
        self.assertEqual(result["gate"], "G8")
        prepare.assert_called_once_with(orchestrator, runtime, "run", "stage", "module")
        self.assertEqual(checkpoints[0][2]["status"], "MANUAL_WAIT")

    def test_worker_prepares_exact_g8_then_consumes_only_matching_approval(self):
        import worker_driver
        from approval_summary import pinned_context, _hash
        material_store = {}
        binding = {"run_id": "run", "build_id": "build", "stage_build_id": "stage", "module": "module",
                   "repositories": [{"module": "baidu/test/product", "revision": "revision"}]}
        input_hash = _hash(binding)
        runtime = self.runtime({"manualParams": None, "hasDynamicParams": False})
        runtime.rerun_input_hash = lambda stage_id, parameters: {
            "ok": True, "input_hash": input_hash, "case_failures": [], "binding": binding}
        runtime._rerun_binding = lambda stage_id, parameters: {"ok": True, "binding": binding}
        calls = []
        runtime.rerun = lambda stage_id, approval, parameters: (
            calls.append((stage_id, approval, parameters)) or {"ok": True})
        state = SimpleNamespace(
            save_idempotency_result=lambda key, value: material_store.setdefault(key, value),
            idempotency_result=lambda key: material_store.get(key),
            events=lambda run: [])
        orchestrator = SimpleNamespace(state=state)
        with patch.object(worker_driver, "_settled_approval_id", return_value=None):
            result = worker_driver._prepare_manual_stage(
                orchestrator, runtime, "run", "stage", "module")
        self.assertEqual(result["gate"], "G8")
        self.assertEqual(result["approval_input_hash"], input_hash)
        self.assertEqual(calls, [])
        with patch.object(orchestrator_module, "_request_approval",
                          return_value={"ok": True, "approval_id": "pending"}) as request:
            delivered = orchestrator_module._deliver_worker_approval(orchestrator, result)
        request.assert_called_once_with(orchestrator, "run", "G8", input_hash)
        self.assertEqual(delivered["parked"], "APPROVAL_WAIT")
        self.assertEqual(pinned_context(orchestrator, "run", "G8", input_hash)["material_status"], "PINNED")
        self.assertEqual(pinned_context(orchestrator, "run", "G8", "wrong")["material_status"], "MISSING")
        with patch.object(worker_driver, "_settled_approval_id", return_value="approved"):
            worker_driver._prepare_manual_stage(orchestrator, runtime, "run", "stage", "module")
        self.assertEqual(calls, [("stage", {"approval_id": "approved", "input_hash": input_hash}, {})])


class OpenTestRepoCrTests(unittest.TestCase):
    """The CR a manual stage should pull is the one still open, not the receipt's.

    Folding T3's own CR into the requirement's existing CR leaves the newest receipt
    pointing at an abandoned change, so the open-CR list has to win.
    """

    PROFILE = {"test_repo": {"module": "baidu/sysip/bgw_auto", "path": "/tmp"}}

    def _with_reviews(self, changes, returncode=0):
        payload = json.dumps({"data": {"changes": changes}})
        calls = []

        class FakeTransport:
            def run(self, command, cwd=None, timeout=None):
                calls.append(command)
                return {"returncode": returncode, "stdout": payload, "stderr": ""}

        original = cli_transport.ProcessTransport
        cli_transport.ProcessTransport = FakeTransport
        try:
            found = orchestrator_module._open_test_repo_cr(self.PROFILE, "run-1")
        finally:
            cli_transport.ProcessTransport = original
        return found, calls

    def test_a_single_open_cr_supersedes_the_receipt(self):
        found, calls = self._with_reviews([{"_number": 122402145}])

        self.assertEqual(found, "122402145")
        self.assertIn("get_repo_reviews", calls[0])
        self.assertIn("baidu/sysip/bgw_auto", calls[0])
        self.assertIn("NEW", calls[0])

    def test_an_ambiguous_repository_leaves_the_receipt_alone(self):
        several, _ = self._with_reviews([{"_number": 1}, {"_number": 2}])
        none_open, _ = self._with_reviews([])
        unusable, _ = self._with_reviews([{"_number": 1}], returncode=1)

        self.assertIsNone(several)
        self.assertIsNone(none_open)
        self.assertIsNone(unusable)

    def test_a_project_without_a_test_repository_is_not_queried(self):
        self.assertIsNone(orchestrator_module._open_test_repo_cr({}, "run-1"))


class RevisionSetTests(unittest.TestCase):
    """What the pipeline is building: the run's CRs at their current patchset.

    A submission receipt records the revision that was pushed then. A repair or a fold
    adds a patchset without writing a new receipt, so a build of the newer patchset was
    unownable and every stage operation refused it.
    """

    PROFILE = {
        "business_repos": [{"module": "baidu/sysip/x86bgw", "path": "/repo/a", "branch": "cdn-url"}],
        "test_repo": {"module": "baidu/nsiqa/x86bgw", "path": "/repo/t", "branch": "pipline_case"},
    }

    def _orchestrator(self, *submissions):
        class Artifacts:
            def artifacts_for_run(self, _run_id):
                return [
                    {"kind": "submission", "content": json.dumps(item).encode("utf-8")}
                    for item in submissions
                ]

        state = type("LegacyState", (), {"events": lambda self, run_id: []})()
        return type("Fake", (), {"artifacts": Artifacts(), "state": state})()

    def _with_reviews(self, reviews):
        original = orchestrator_module._open_repo_reviews
        orchestrator_module._open_repo_reviews = lambda module, path: reviews.get(str(module))
        try:
            return orchestrator_module._ipipe_revision_set(
                self._orchestrator(*self.submissions), "run-1", self.PROFILE
            )
        finally:
            orchestrator_module._open_repo_reviews = original

    def setUp(self):
        self.submissions = [{
            "module": "baidu/sysip/x86bgw",
            "change_number": "122396573",
            "revision_set": {
                "business": {"module": "baidu/sysip/x86bgw", "branch": "cdn-url", "revision": "old-business"},
                "test": {"module": "baidu/nsiqa/x86bgw", "branch": "pipline_case", "revision": "old-test"},
            },
        }]

    def test_a_newer_patchset_replaces_the_receipt_revision_and_is_reported(self):
        derived = self._with_reviews({
            "baidu/sysip/x86bgw": [{"_number": 122396573, "current_revision": "new-business"}],
            "baidu/nsiqa/x86bgw": [{"_number": 122402145, "current_revision": "new-test"}],
        })

        revisions = {
            item["module"]: item["revision"]
            for item in derived["revisions"]["repositories"]
        }
        self.assertTrue(derived["ok"])
        self.assertEqual(revisions["baidu/sysip/x86bgw"], "new-business")
        # The test repository's open CR supersedes the receipt's folded-away number.
        self.assertEqual(revisions["baidu/nsiqa/x86bgw"], "new-test")
        self.assertEqual(
            derived["drift"]["baidu/sysip/x86bgw"],
            {"submitted": "old-business", "current": "new-business"},
        )

    def test_an_unanswerable_repository_keeps_the_receipt_revision(self):
        derived = self._with_reviews({})

        revisions = {
            item["module"]: item["revision"]
            for item in derived["revisions"]["repositories"]
        }
        self.assertEqual(revisions["baidu/sysip/x86bgw"], "old-business")
        self.assertEqual(revisions["baidu/nsiqa/x86bgw"], "old-test")
        self.assertEqual(derived["drift"], {})

    def test_a_module_with_no_revision_at_all_is_named_not_guessed(self):
        self.submissions = [{
            "module": "baidu/sysip/x86bgw", "change_number": "122396573",
            "revision_set": {
                "business": {"module": "baidu/sysip/x86bgw", "branch": "cdn-url", "revision": "only-business"},
            },
        }]

        derived = self._with_reviews({})

        self.assertFalse(derived["ok"])
        self.assertEqual(derived["reason_code"], "REVISION_UNRESOLVED")
        self.assertEqual(derived["module"], "baidu/nsiqa/x86bgw")

    def test_the_revision_set_id_follows_the_revisions(self):
        first = self._with_reviews({})
        moved = self._with_reviews({
            "baidu/sysip/x86bgw": [{"_number": 122396573, "current_revision": "new-business"}],
        })

        self.assertNotEqual(
            first["revisions"]["revision_set_id"], moved["revisions"]["revision_set_id"]
        )


if __name__ == "__main__":
    unittest.main()
