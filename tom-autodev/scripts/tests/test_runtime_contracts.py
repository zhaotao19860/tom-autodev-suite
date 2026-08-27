import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.icode_client import IcodeClient
from clients.ipipe_client import IpipeClient
from orchestrator import Orchestrator


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def preflight(self, repo):
        self.calls.append(("preflight", repo))
        return {"reason_code": "OK"}

    def submit(self, change, approval):
        self.calls.append(("submit", change, approval))
        return {"reason_code": "OK", "change_number": "1"}

    def discover(self, profile, revisions):
        self.calls.append(("discover", profile, revisions))
        return {"reason_code": "OK", "build_id": "b1"}

    def trigger(self, profile, revisions, approval):
        self.calls.append(("trigger", profile, revisions, approval))
        return {"reason_code": "OK", "build_id": "b1"}

    def monitor(self, build_id, deadline):
        self.calls.append(("monitor", build_id, deadline))
        return {"reason_code": "OK", "status": "SUCCESS"}

    def rerun(self, stage_id, approval):
        self.calls.append(("rerun", stage_id, approval))
        return {"reason_code": "OK", "stage_build_id": stage_id}

    def verify_release(self, build_id, revisions):
        self.calls.append(("verify_release", build_id, revisions))
        return {"reason_code": "OK", "release_id": "r1"}


class RuntimeAdapterTests(unittest.TestCase):
    def test_callable_backends_cannot_bypass_runtime_write_gates(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "ICODE_RUNTIME_REQUIRED"):
            IcodeClient(lambda payload: calls.append(("icode", payload)))
        with self.assertRaisesRegex(ValueError, "IPIPE_RUNTIME_REQUIRED"):
            IpipeClient(lambda action, payload: calls.append((action, payload)))
        self.assertEqual(calls, [])

    def test_icode_client_exposes_preflight_and_approval_bound_submit(self):
        runtime = FakeRuntime()
        client = IcodeClient(runtime=runtime)
        self.assertEqual(client.preflight(Path("/repo"))["reason_code"], "OK")
        result = client.submit({"id": "change"}, {"approval_id": "a1"})
        self.assertEqual(result["change_number"], "1")
        self.assertEqual([call[0] for call in runtime.calls], ["preflight", "submit"])

    def test_ipipe_client_exposes_complete_runtime_contract(self):
        runtime = FakeRuntime()
        client = IpipeClient(runtime=runtime)
        self.assertEqual(client.discover({"p": 1}, {"r": 1})["build_id"], "b1")
        self.assertEqual(client.trigger({"p": 1}, {"r": 1}, {"approval_id": "a"})["build_id"], "b1")
        self.assertEqual(client.monitor("b1", "deadline")["status"], "SUCCESS")
        self.assertEqual(client.rerun("s1", {"approval_id": "a"})["stage_build_id"], "s1")
        self.assertEqual(client.verify_release("b1", {"r": 1})["release_id"], "r1")


class RuntimeOrchestratorTests(unittest.TestCase):
    def test_runtime_failure_enriches_diagnose_evidence_with_collaboration_route(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            expected = {
                "REVISION_MISMATCH": "code",
                "INTERFACE_FAILURE": "interface",
                "TEST_FAILURE": "test-case",
                "ENVIRONMENT_FAILURE": "environment",
                "MIXED_FAILURE": "mixed",
                "PIPELINE_TRANSIENT": "platform",
                "AUTH_REQUIRED": "auth",
                "RELEASE_RULE_MISMATCH": "release-rule",
            }
            for index, (reason, category) in enumerate(expected.items()):
                run_id = f"run-{index}"
                orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})
                result = orchestrator.route_failure(run_id, reason, {"signature": reason})
                event = orchestrator.status(run_id)["events"][-1]
                self.assertEqual(result["collaboration_category"], category)
                self.assertEqual(event["payload"]["evidence"]["category"], category)

    def test_actual_runtime_reason_families_all_route_to_diagnose(self):
        reasons = [
            "CR_IDENTITY_CONFLICT",
            "SUBMIT_REJECTED",
            "TRIGGER_RESPONSE_INVALID",
            "RERUN_CONFLICT",
            "MONITOR_TIMEOUT",
            "BUILD_QUERY_FAILED",
            "RELEASE_QUERY_FAILED",
            "ICODE_LOGIN_REQUIRED",
        ]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            for index, reason in enumerate(reasons):
                run_id = f"emitted-{index}"
                orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})
                result = orchestrator.route_failure(run_id, reason, {"signature": reason})
                self.assertEqual(result["state"], "DIAGNOSE", reason)

    def test_pipeline_failure_classification_selects_task5_collaboration_role(self):
        cases = [
            ("TEST_FAILURE", "test-case"),
            ("ENVIRONMENT_FAILURE", "environment"),
            ("MIXED_FAILURE", "mixed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            for index, (classification, category) in enumerate(cases):
                run_id = f"classified-{index}"
                orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})
                result = orchestrator.route_failure(
                    run_id,
                    "PIPELINE_FAILED",
                    {"classification": classification, "signature": classification},
                )
                self.assertEqual(result["state"], "DIAGNOSE")
                self.assertEqual(result["collaboration_category"], category)

    def test_login_permission_revision_interface_and_release_map_to_expected_owners(self):
        cases = [
            ("ICODE_LOGIN_REQUIRED", {}, "auth"),
            ("PERMISSION_DENIED", {}, "platform"),
            ("REVISION_MISMATCH", {}, "code"),
            ("INTERFACE_FAILURE", {}, "interface"),
            ("RELEASE_RULE_MISMATCH", {}, "release-rule"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            for index, (reason, evidence, category) in enumerate(cases):
                run_id = f"owned-{index}"
                orchestrator.state.transition(run_id, "REVIEW", {"test_setup": True})
                result = orchestrator.route_failure(run_id, reason, evidence)
                self.assertEqual(result["collaboration_category"], category, reason)

    def test_runtime_factories_reuse_run_state_ledger_and_validate_run(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = Orchestrator(Path(directory))
            orchestrator.state.transition("run-1", "REVIEW", {"test_setup": True})
            icode = orchestrator.icode_runtime(
                "run-1",
                worktree_bindings={},
                system_skill_path=Path(directory),
                argv_transport=object(),
            )
            missing = orchestrator.icode_runtime("missing")
        self.assertIs(icode.state, orchestrator.state)
        self.assertIs(icode.approvals, orchestrator.approvals)
        self.assertIs(icode.artifacts, orchestrator.artifacts)
        self.assertIs(icode.workspaces, orchestrator.workspaces)
        self.assertEqual(missing["reason_code"], "RUN_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
