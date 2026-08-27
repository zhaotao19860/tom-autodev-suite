import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import Orchestrator


PROFILE = {
    "project_id": "bgw",
    "business_repos": [
        {"path": "/repo/business", "module": "bgw", "branch": "main", "lock": "bgw-main"}
    ],
    "test_repo": {"path": "/repo/tests", "module": "bgw-tests", "branch": "main", "lock": "bgw-tests-main"},
    "language_skill": "/skills/tom-lang-c-cpp",
    "project_skill": "/skills/tom-project-bgw",
    "knowledge_sources": [{
        "provider": "ku", "repository": "knowledge", "revision": "r1",
        "search_scope": "project", "priority": 1, "repo_id": "sX0BTOBWJX",
        "parent_doc_id": "I15ClP2KW4ZGAK",
    }],
    "review_provider": {"kind": "source-only", "command": "review --check"},
    "pipeline_profile": {
        "pipeline_id": "bgw-pipeline", "allowed_parameters": [],
        "stage_classes": ["compile", "unit", "regression", "integration"],
        "release_rule": "manual-approval",
    },
    "environment_profile": {
        "runner": "linux", "os_arch": "linux/amd64", "image_digest": "sha256:env",
        "toolchain_digest": "sha256:tools", "hardware_or_simulator": "hardware",
        "data": "fixture", "services": "fixture", "capacity": "reserved",
    },
    "approval_channels": {
        "comate": {"channel": "comate-review"}, "infoflow": {"channel": "group"},
        "role_members": {
            "development": ["dev@example.test"], "test": ["tester@example.test"],
            "project": ["owner@example.test"],
        },
    },
}


class ReadOnlyProbe:
    def __init__(self, name, result=None):
        self.name = name
        self.result = result or {"ok": True, "reason_code": "OK", "detail": f"{name}-ready"}
        self.calls = []

    def query(self, context):
        self.calls.append(copy.deepcopy(context))
        return copy.deepcopy(self.result)

    def create(self, *_args, **_kwargs):
        raise AssertionError("preflight must not create")

    def update(self, *_args, **_kwargs):
        raise AssertionError("preflight must not update")

    def submit(self, *_args, **_kwargs):
        raise AssertionError("preflight must not submit")

    def trigger(self, *_args, **_kwargs):
        raise AssertionError("preflight must not trigger")


class LivePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._write_profile()
        self.orchestrator = Orchestrator(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def probes(self):
        return {name: ReadOnlyProbe(name) for name in (
            "icafe", "ku", "icode", "review", "infoflow", "ipipe"
        )}

    def test_all_components_ready_without_state_intents_or_external_writes(self):
        probes = self.probes()
        before = (self.orchestrator.state.events("unused"), self.orchestrator.state.pending_intents("unused"))

        result = self.orchestrator.preflight("bgw", probes=probes)

        after = (self.orchestrator.state.events("unused"), self.orchestrator.state.pending_intents("unused"))
        self.assertEqual(result["status"], "READY")
        self.assertEqual(sorted(result["components"]), sorted(probes))
        self.assertEqual(before, after)
        self.assertTrue(all(len(probe.calls) == 1 for probe in probes.values()))
        self.assertNotIn("role_members", str([probe.calls for probe in probes.values()]))

    def test_component_failure_is_fail_closed_and_redacted(self):
        probes = self.probes()
        probes["ku"] = ReadOnlyProbe("ku", {
            "ok": False,
            "reason_code": "KU_ACCESS_DENIED",
            "diagnostic": {"token": "top-secret", "owner": "owner@example.test"},
        })

        result = self.orchestrator.preflight("bgw", probes=probes)

        encoded = str(result)
        self.assertEqual(result["status"], "PREFLIGHT_FAILED")
        self.assertEqual(result["components"]["ku"]["reason_code"], "PREFLIGHT_QUERY_FAILED")
        self.assertNotIn("top-secret", encoded)
        self.assertNotIn("owner@example.test", encoded)
        self.assertEqual(self.orchestrator.state.pending_intents("unused"), [])

    def test_missing_or_unconfirmed_project_stops_before_any_probe(self):
        probes = self.probes()

        result = self.orchestrator.preflight("unknown", probes=probes)

        self.assertEqual(result["status"], "PROJECT_NOT_READY")
        self.assertTrue(all(probe.calls == [] for probe in probes.values()))

    def test_missing_probe_is_not_inferred(self):
        probes = self.probes()
        probes.pop("ipipe")

        result = self.orchestrator.preflight("bgw", probes=probes)

        self.assertEqual(result["status"], "PROJECT_NOT_READY")
        self.assertEqual(result["missing"], ["preflight.ipipe"])
        self.assertTrue(all(probe.calls == [] for probe in probes.values()))

    def test_untrusted_reason_text_cannot_bypass_output_redaction(self):
        probes = self.probes()
        probes["icafe"] = ReadOnlyProbe("icafe", {
            "ok": False,
            "reason_code": "owner@example.test token=top-secret",
        })

        result = self.orchestrator.preflight("bgw", probes=probes)

        self.assertEqual(result["components"]["icafe"]["reason_code"], "PREFLIGHT_QUERY_FAILED")
        self.assertNotIn("owner@example.test", str(result))
        self.assertNotIn("top-secret", str(result))

    def test_nested_string_credentials_urls_and_phones_are_redacted(self):
        probes = self.probes()
        probes["review"] = ReadOnlyProbe("review", {
            "ok": False,
            "reason_code": "REVIEW_QUERY_FAILED",
            "nested": [
                "Authorization: Bearer top-secret",
                {"detail": "api_key=key-value token=token-value"},
                "https://user:pass@example.test/path?access_key=url-key",
                "call 13800138000",
            ],
        })

        result = self.orchestrator.preflight("bgw", probes=probes)
        encoded = str(result)

        for secret in ("top-secret", "key-value", "token-value", "user:pass", "url-key", "13800138000"):
            self.assertNotIn(secret, encoded)

    def test_secret_shaped_uppercase_reason_codes_are_never_restored(self):
        for reason in ("TOKEN_TOP_SECRET", "AUTHORIZATION_BEARER_SECRET"):
            probes = self.probes()
            probes["ku"] = ReadOnlyProbe("ku", {
                "ok": False,
                "reason_code": reason,
            })

            with self.subTest(reason=reason):
                result = self.orchestrator.preflight("bgw", probes=probes)
                encoded = str(result)

                self.assertEqual(
                    result["components"]["ku"]["reason_code"],
                    "PREFLIGHT_QUERY_FAILED",
                )
                self.assertNotIn(reason, encoded)

    def test_requested_project_must_match_profile_identity_before_queries(self):
        profile_path = self.root / "config" / "projects" / "bgw.yaml"
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile["project_id"] = "xflow"
        profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        probes = self.probes()

        result = self.orchestrator.preflight("bgw", probes=probes)

        self.assertEqual(result["status"], "PROJECT_NOT_READY")
        self.assertEqual(result["reason_code"], "PROJECT_PROFILE_MISMATCH")
        self.assertTrue(all(probe.calls == [] for probe in probes.values()))

    def test_live_probe_objects_expose_no_remote_write_methods(self):
        from preflight import live_probes

        probes = live_probes()

        for name, probe in probes.items():
            with self.subTest(component=name):
                self.assertTrue(callable(getattr(probe, "query", None)))
                for method in ("create", "update", "comment", "submit", "trigger", "release", "rerun"):
                    self.assertFalse(callable(getattr(probe, method, None)))

    def test_ku_live_probe_uses_documented_read_only_pagination_flags(self):
        from preflight import _KuProbe

        class Transport:
            def __init__(self): self.calls = []
            def run(self, argv, **options): self.calls.append((argv, options)); return {"returnCode": 200}

        transport = Transport()
        result = _KuProbe(transport).query({
            "repo_id": "sX0BTOBWJX", "parent_doc_id": "I15ClP2KW4ZGAK"
        })

        self.assertTrue(result["ok"])
        self.assertEqual(transport.calls[0][0][1:], [
            "query-repo", "--repo-id", "sX0BTOBWJX", "--parent-doc-id", "I15ClP2KW4ZGAK",
            "--page-num", "1", "--page-size", "1",
        ])

    def test_each_live_probe_uses_only_its_exact_query_contract(self):
        from preflight import (
            _IcafeProbe, _IcodeProbe, _InfoflowProbe, _IpipeProbe, _ReviewProbe,
        )

        class CliTransport:
            def __init__(self): self.calls = []
            def run(self, argv, **options): self.calls.append((list(argv), options)); return {"ok": True}

        icafe_transport = CliTransport()
        self.assertTrue(_IcafeProbe(icafe_transport).query({})["ok"])
        self.assertEqual([call[0] for call in icafe_transport.calls], [
            ["icafe-cli", "version"], ["icafe-cli", "login", "status"],
        ])
        self.assertTrue(all(call[1] == {"expect_json": False} for call in icafe_transport.calls))

        infoflow_transport = CliTransport()
        infoflow = _InfoflowProbe(infoflow_transport, setup_path="/readonly/setup.sh")
        self.assertTrue(infoflow.query({"channel": "group"})["ok"])
        self.assertEqual(infoflow_transport.calls, [
            (["/readonly/setup.sh", "--check"], {"expect_json": False})
        ])

        review = _ReviewProbe(executable_resolver=lambda command: "/bin/review" if command == "review" else None)
        self.assertTrue(review.query({"kind": "source-only", "command": "review --check"})["ok"])

        class IpipeClient:
            def __init__(self): self.calls = []
            def pipelines_by_module(self, module):
                self.calls.append(("pipelines_by_module", module))
                return [{"id": "pipe-1", "moduleName": "bgw", "pipelineName": "BranchPipeline"}]
            def get_pipeline_by_id(self, _pipeline_id):
                raise AssertionError("preflight must not fetch the full pipeline conf")
            def trigger_by_revision(self, *_args): raise AssertionError("preflight must not trigger")

        ipipe_client = IpipeClient()
        ipipe = _IpipeProbe(client_factory=lambda username: ipipe_client, username="tester")
        self.assertTrue(ipipe.query({"pipeline_id": "pipe-1", "module": "bgw"})["ok"])
        self.assertEqual(ipipe_client.calls, [("pipelines_by_module", "bgw")])

        class IcodeTransport:
            def __init__(self): self.calls = []
            def run(self, argv, **options):
                self.calls.append((list(argv), options))
                stdout = "icode 0.1.11" if argv[-1] == "version" else "Already logged in as: tester (ugate)"
                return {"returncode": 0, "stdout": stdout, "stderr": ""}

        icode_transport = IcodeTransport()
        icode = _IcodeProbe(
            skill_path=self.root, binary_resolver=lambda _name: "/bin/icode",
            transport=icode_transport,
        )
        result = icode.query({"repositories": [{"path": "/repo", "module": "bgw", "branch": "main"}]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(icode_transport.calls, [
            (["/bin/icode", "version"], {}),
            (["/bin/icode", "login"], {"cwd": Path("/repo"), "stdin_closed": True}),
        ])

    def test_icode_process_transport_closes_stdin_and_never_supplies_a_token(self):
        from preflight import _ArgvQueryTransport

        completed = subprocess.CompletedProcess(
            ["/bin/icode-cli", "login"], 0,
            stdout="Already logged in as: tester (ugate)\n", stderr="",
        )
        with patch("subprocess.run", return_value=completed) as run:
            result = _ArgvQueryTransport().run(
                ["/bin/icode-cli", "login"], cwd=Path("/repo"), stdin_closed=True
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("input", run.call_args.kwargs)
        self.assertNotIn("--token", run.call_args.args[0])

    def test_icode_probe_rejects_a_login_that_was_not_already_authenticated(self):
        from preflight import _IcodeProbe

        class Transport:
            def run(self, argv, **_options):
                return {
                    "returncode": 0,
                    "stdout": "icode-cli 1.0" if argv[-1] == "--version" else "Logged in as: tester (ugate)",
                    "stderr": "",
                }

        result = _IcodeProbe(
            skill_path=self.root, binary_resolver=lambda _name: "/bin/icode-cli",
            transport=Transport(),
        ).query({"repositories": [{"path": "/repo", "module": "bgw", "branch": "main"}]})

        self.assertEqual(result["reason_code"], "ICODE_LOGIN_STATUS_UNSAFE")

    def _write_profile(self):
        profile = copy.deepcopy(PROFILE)
        for key in ("business", "tests"):
            subprocess.run(["git", "init", "-q", str(self.root / key)], check=True)
        for key in ("language", "project"):
            path = self.root / key
            path.mkdir()
            (path / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
        profile["business_repos"][0]["path"] = str(self.root / "business")
        profile["test_repo"]["path"] = str(self.root / "tests")
        profile["language_skill"] = str(self.root / "language")
        profile["project_skill"] = str(self.root / "project")
        target = self.root / "config" / "projects" / "bgw.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(yaml.safe_dump(profile), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
