import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile_wizard import (
    build_profile,
    confirmed_inputs,
    discover_branches,
    discover_pipeline_ids,
    load_template,
    unfilled_placeholders,
    write_requirement_profile,
)
from project_registry import (
    acceptable_profile_paths,
    requirement_profile_path,
    resolve_active_profile_path,
    validate_profile,
)


ANSWERS = {
    "branches": {
        "baidu/sysip/x86bgw": "feature-a",
        "baidu/sysip/bgwagent": "feature-a",
    },
    "test_branch": "pipline_case",
    "revisions": {
        "baidu/sysip/x86bgw": "aaa111",
        "baidu/sysip/bgwagent": "bbb222",
    },
    "ku": [{"repo_id": "R1", "parent_doc_id": "P1", "revision": "snapshot-x"}],
    "pipeline_ids": {
        "baidu/sysip/x86bgw": "348102",
        "baidu/sysip/bgwagent": "348142",
        "baidu/nsiqa/x86bgw": "504074",
    },
    "members": {
        "development": ["dev@baidu.com"],
        "test": ["qa@baidu.com"],
        "project": ["pm@baidu.com"],
    },
    "environment_provenance": {
        "status": "VERIFIED",
        "runner_identity": "test-runner-1",
        "image_toolchain_identity": "sha256:test-toolchain",
        "verified_at": "2026-09-29T00:00:00Z",
        "verifier": "test-owner",
        "evidence_ref": "ipipe:evidence:test-runner-1",
    },
}


def confirmed_answers() -> dict:
    answers = dict(ANSWERS)
    profile = build_profile(load_template("bgw"), answers)
    answers["confirmed_inputs"] = confirmed_inputs(profile)
    answers["confirmed_by"] = "test-owner"
    return answers


class BuildProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = build_profile(load_template("bgw"), ANSWERS)

    def test_no_placeholder_survives_a_complete_answer_set(self):
        self.assertEqual(unfilled_placeholders(self.profile), set())

    def test_branch_drives_lock_last_segment(self):
        x86 = next(r for r in self.profile["business_repos"] if r["module"] == "baidu/sysip/x86bgw")
        self.assertEqual(x86["branch"], "feature-a")
        self.assertEqual(x86["lock"], "bgw/x86bgw/feature-a")
        self.assertEqual(self.profile["test_repo"]["lock"], "bgw/x86bgw-qa/pipline_case")

    def test_graph_and_source_revisions_follow_the_repo_revision(self):
        by_repo = {
            (s["provider"], s["repository"]): s["revision"]
            for s in self.profile["knowledge_sources"]
            if s["provider"] in ("repository", "gitnexus")
        }
        self.assertEqual(by_repo[("repository", "baidu/sysip/x86bgw")], "aaa111")
        self.assertEqual(by_repo[("gitnexus", "x86bgw")], "aaa111")
        self.assertEqual(by_repo[("gitnexus", "bgwagent")], "bbb222")

    def test_release_rule_branch_is_the_primary_business_branch(self):
        self.assertIn("feature-a", self.profile["pipeline_profile"]["release_rule"])
        self.assertNotIn("{primary_branch}", self.profile["pipeline_profile"]["release_rule"])

    def test_members_come_from_answers(self):
        self.assertEqual(
            self.profile["approval_channels"]["role_members"]["development"], ["dev@baidu.com"]
        )

    def test_review_provider_is_inlined_away(self):
        self.assertNotIn("review_provider", self.profile)

    def test_pipeline_ids_fill_from_discovery_answers(self):
        pipelines = self.profile["pipeline_profile"]["pipelines"]
        by_module = {p["module"]: p["pipeline_id"] for p in pipelines}
        self.assertEqual(by_module["baidu/sysip/x86bgw"], "348102")
        self.assertEqual(by_module["baidu/sysip/bgwagent"], "348142")
        self.assertEqual(by_module["baidu/nsiqa/x86bgw"], "504074")
        # Top-level id follows the primary business module.
        self.assertEqual(self.profile["pipeline_profile"]["pipeline_id"], "348102")

    def test_release_rule_no_longer_hardcodes_pipeline_ids(self):
        rule = self.profile["pipeline_profile"]["release_rule"]
        for stale in ("348102", "348142", "504074"):
            self.assertNotIn(stale, rule)

    def test_a_missing_branch_leaves_a_reported_placeholder(self):
        answers = {**ANSWERS, "branches": {"baidu/sysip/bgwagent": "feature-a"}}
        profile = build_profile(load_template("bgw"), answers)
        self.assertIn("__BRANCH__", unfilled_placeholders(profile))

    def test_template_environment_without_platform_provenance_is_not_ready(self):
        profile = build_profile(load_template("bgw"), {
            key: value for key, value in ANSWERS.items()
            if key != "environment_provenance"
        })
        result = validate_profile(profile, check_paths=False)
        self.assertFalse(result["ready"])
        self.assertIn("environment_profile.provenance.status", result["invalid"])


class WriteRequirementProfileTests(unittest.TestCase):
    def test_each_card_writes_its_own_file_and_never_clobbers_a_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_requirement_profile(
                "bgw", "BGW-1995", confirmed_answers(), config_root=root, check_paths=False
            )
            second = write_requirement_profile(
                "bgw", "BGW-2000", confirmed_answers(), config_root=root, check_paths=False
            )
            self.assertTrue(first["ready"], first)
            self.assertTrue(second["ready"], second)
            path_a = requirement_profile_path("bgw", "BGW-1995", root)
            path_b = requirement_profile_path("bgw", "BGW-2000", root)
            self.assertNotEqual(path_a, path_b)
            self.assertTrue(path_a.is_file())
            self.assertTrue(path_b.is_file())

    def test_rewriting_the_same_card_requires_the_current_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_requirement_profile(
                "bgw", "BGW-1995", confirmed_answers(), config_root=root, check_paths=False
            )
            again = write_requirement_profile(
                "bgw", "BGW-1995", confirmed_answers(), config_root=root, check_paths=False
            )
            # Same content, so save_profile treats it as an idempotent rewrite.
            self.assertTrue(again["ready"], again)

    def test_unfilled_template_is_refused_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            result = write_requirement_profile(
                "bgw", "BGW-1", {}, config_root=Path(directory), check_paths=False
            )
            self.assertEqual(result["reason_code"], "TEMPLATE_PLACEHOLDER_UNFILLED")


class RequirementProfilePathTests(unittest.TestCase):
    def test_path_is_grouped_under_the_project(self):
        path = requirement_profile_path("bgw", "BGW-1995", "/tmp/root")
        self.assertEqual(path, Path("/tmp/root/config/projects/bgw/BGW-1995.yaml"))

    def test_a_card_with_a_separator_is_refused(self):
        with self.assertRaises(ValueError):
            requirement_profile_path("bgw", "../etc/passwd", "/tmp/root")

    def test_resolve_prefers_per_card_then_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "config" / "projects" / "bgw.yaml"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("project_id: bgw\n", encoding="utf-8")
            self.assertEqual(resolve_active_profile_path("bgw", "BGW-1995", root), legacy)
            per_card = requirement_profile_path("bgw", "BGW-1995", root)
            per_card.parent.mkdir(parents=True, exist_ok=True)
            per_card.write_text("project_id: bgw\n", encoding="utf-8")
            self.assertEqual(resolve_active_profile_path("bgw", "BGW-1995", root), per_card)

    def test_both_per_card_and_legacy_paths_are_acceptable_to_the_guard(self):
        allowed = acceptable_profile_paths("bgw", "BGW-1995", "/tmp/root")
        self.assertIn("/tmp/root/config/projects/bgw.yaml", allowed)
        self.assertIn("/tmp/root/config/projects/bgw/BGW-1995.yaml", allowed)


def _init_repo(path: Path, branch: str) -> str:
    path.mkdir(parents=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", branch], check=True, env={**__import__("os").environ, **env})
    (path / "f").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env={**__import__("os").environ, **env})
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env={**__import__("os").environ, **env})
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


class DiscoverBranchesTests(unittest.TestCase):
    def test_branches_and_revisions_come_from_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = _init_repo(root / "biz", "feature-x")
            _init_repo(root / "qa", "pipline_case")
            template = {
                "business_repos": [{"module": "m/biz", "path": str(root / "biz")}],
                "test_repo": {"module": "m/qa", "path": str(root / "qa")},
            }
            suggestion = discover_branches(template)
            self.assertEqual(suggestion["branches"]["m/biz"], "feature-x")
            self.assertEqual(suggestion["revisions"]["m/biz"], head)
            self.assertEqual(suggestion["test_branch"], "pipline_case")
            self.assertEqual(suggestion["unresolved"], [])

    def test_a_non_repo_path_is_reported_unresolved(self):
        template = {
            "business_repos": [{"module": "m/biz", "path": "/nonexistent/xyz"}],
            "test_repo": {},
        }
        suggestion = discover_branches(template)
        self.assertIn("m/biz", suggestion["unresolved"])


class DiscoverPipelineTests(unittest.TestCase):
    def test_transport_is_bounded_and_response_is_schema_checked(self):
        calls = []

        def transport(cli, module, timeout):
            calls.append((cli, module, timeout))
            return {
                "entities": [
                    {"pipelineName": "ChangePipeline_zyd", "id": "wrong"},
                    {"pipelineName": "ChangePipeline", "id": 123},
                ]
            }

        with patch("profile_wizard._ipipe_cli", return_value="/fake/ipipe"):
            result = discover_pipeline_ids(
                ["baidu/sysip/x86bgw"], transport=transport, timeout_seconds=3.5
            )
        self.assertEqual(result["pipeline_ids"], {"baidu/sysip/x86bgw": "123"})
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(calls, [("/fake/ipipe", "baidu/sysip/x86bgw", 3.5)])
        self.assertEqual(result["evidence"][0]["status"], "MATCHED")

    def test_ambiguous_and_malformed_responses_fail_closed(self):
        def transport(_cli, module, _timeout):
            if module == "ambiguous":
                return {"entities": [
                    {"pipelineName": "ChangePipeline", "id": "a"},
                    {"pipelineName": "ChangePipeline", "id": "b"},
                ]}
            return {"unexpected": []}

        with patch("profile_wizard._ipipe_cli", return_value="/fake/ipipe"):
            result = discover_pipeline_ids(["ambiguous", "malformed"], transport=transport)
        self.assertEqual(result["pipeline_ids"], {})
        self.assertEqual(result["unresolved"], ["ambiguous", "malformed"])
        self.assertEqual(
            [item["status"] for item in result["evidence"]],
            ["AMBIGUOUS", "INVALID_RESPONSE"],
        )


if __name__ == "__main__":
    unittest.main()
