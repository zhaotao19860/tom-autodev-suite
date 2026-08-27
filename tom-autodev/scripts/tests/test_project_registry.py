import copy
import hashlib
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_registry import load_profile, save_profile, validate_profile


REQUIRED_PROFILE = {
    "project_id": "bgw",
    "business_repos": [
        {"path": "/repo/business", "module": "bgw", "branch": "main", "lock": "bgw-main"}
    ],
    "test_repo": {
        "path": "/repo/tests",
        "module": "bgw-tests",
        "branch": "main",
        "lock": "bgw-tests-main",
    },
    "language_skill": "/skills/tom-lang-c-cpp",
    "project_skill": "/skills/tom-project-bgw",
    "knowledge_sources": [
        {
            "provider": "ku",
            "repository": "knowledge",
            "revision": "r1",
            "search_scope": "project",
            "priority": 1,
            "repo_id": "repo-1",
            "parent_doc_id": "parent-1",
        }
    ],
    "review_provider": {"kind": "source-only", "command": "review"},
    "pipeline_profile": {
        "pipeline_id": "bgw-pipeline",
        "allowed_parameters": [],
        "stage_classes": ["unit"],
        "release_rule": "manual-approval",
    },
    "environment_profile": {
        "runner": "linux",
        "os_arch": "linux/amd64",
        "image_digest": "sha256:env",
        "toolchain_digest": "sha256:tools",
        "hardware_or_simulator": "simulator",
        "data": "test-data",
        "services": "none",
        "capacity": "small",
    },
    "approval_channels": {
        "comate": {"channel": "comate-review"},
        "infoflow": {"channel": "group-1"},
        "role_members": {
            "development": ["developer@example.test"],
            "test": ["tester@example.test"],
            "project": ["manager@example.test"],
        },
    },
}


class ProjectRegistryTests(unittest.TestCase):
    def test_complete_profile_is_ready(self):
        result = validate_profile(REQUIRED_PROFILE, check_paths=False)

        self.assertEqual(result["reason_code"], "READY")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["invalid"], [])

    def test_missing_test_repo_blocks_setup(self):
        profile = dict(REQUIRED_PROFILE)
        profile.pop("test_repo")

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(result["missing"], ["test_repo"])

    def test_missing_nested_repo_fields_are_reported_in_order(self):
        profile = copy.deepcopy(REQUIRED_PROFILE)
        profile["business_repos"] = [{"path": "/repo/business"}]

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(
            result["missing"],
            ["business_repos[0].branch", "business_repos[0].lock", "business_repos[0].module"],
        )

    def test_approval_channels_require_named_nonempty_references(self):
        profile = copy.deepcopy(REQUIRED_PROFILE)
        profile["approval_channels"]["comate"] = {"channel": "", "extra": "no"}
        profile["approval_channels"]["infoflow"] = {"channel": []}

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(
            result["invalid"],
            ["approval_channels.comate.extra", "approval_channels.infoflow.channel"],
        )
        self.assertEqual(result["missing"], ["approval_channels.comate.channel"])

    def test_knowledge_sources_require_ku_but_allow_supported_additional_sources(self):
        profile = copy.deepcopy(REQUIRED_PROFILE)
        profile["knowledge_sources"].append(
            {
                "provider": "gitnexus",
                "repository": "graph",
                "revision": "r2",
                "search_scope": "all",
                "priority": 2,
            }
        )

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(result["reason_code"], "READY")
        profile["knowledge_sources"][0]["provider"] = "unknown"
        invalid = validate_profile(profile, check_paths=False)
        self.assertIn("knowledge_sources[0].provider", invalid["invalid"])
        profile["knowledge_sources"] = [profile["knowledge_sources"][1]]
        missing_ku = validate_profile(profile, check_paths=False)
        self.assertEqual(missing_ku["missing"], ["knowledge_sources.ku"])

    def test_rejects_invalid_capabilities_and_secret_values(self):
        profile = copy.deepcopy(REQUIRED_PROFILE)
        profile["review_provider"] = {"kind": "remote", "command": "review"}
        profile["approval_channels"]["comate"] = {}
        profile["pipeline_profile"].pop("allowed_parameters")
        profile["environment_profile"].pop("capacity")
        profile["knowledge_sources"][0]["parent_doc_id"] = ""
        profile["access_token"] = "not-to-be-disclosed"

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(
            result["missing"],
            [
                "approval_channels.comate.channel",
                "environment_profile.capacity",
                "knowledge_sources[0].parent_doc_id",
                "pipeline_profile.allowed_parameters",
            ],
        )
        self.assertEqual(
            result["invalid"],
            ["access_token", "review_provider.kind"],
        )
        self.assertNotIn("not-to-be-disclosed", repr(result))

    def test_path_validation_requires_git_worktrees_and_skill_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            business = root / "business"
            tests = root / "tests"
            language_skill = root / "language-skill"
            project_skill = root / "project-skill"
            for repo in (business, tests):
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for skill in (language_skill, project_skill):
                skill.mkdir()
                (skill / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
            profile = copy.deepcopy(REQUIRED_PROFILE)
            profile["business_repos"][0]["path"] = str(business)
            profile["test_repo"]["path"] = str(tests)
            profile["language_skill"] = str(language_skill)
            profile["project_skill"] = str(project_skill)

            result = validate_profile(profile)

        self.assertEqual(result["reason_code"], "READY")

    def test_path_validation_rejects_dot_git_marker_that_is_not_a_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = _profile_with_existing_paths(root)
            fake_repo = root / "not-a-repository"
            (fake_repo / ".git").mkdir(parents=True)
            profile["business_repos"][0]["path"] = str(fake_repo)

            result = validate_profile(profile)

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertIn("business_repos[0].path", result["invalid"])

    def test_path_validation_rejects_nested_and_symlinked_test_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = _profile_with_existing_paths(root)
            business = Path(profile["business_repos"][0]["path"])
            nested = business / "product-tests"
            nested.mkdir()
            profile["test_repo"]["path"] = str(nested)

            nested_result = validate_profile(profile)

            alias = root / "business-alias"
            alias.symlink_to(business, target_is_directory=True)
            profile["test_repo"]["path"] = str(alias)
            alias_result = validate_profile(profile)

        self.assertIn("test_repo.path", nested_result["invalid"])
        self.assertIn("test_repo.path", alias_result["invalid"])

    def test_validation_paths_are_canonical_sorted_and_unique(self):
        profile = copy.deepcopy(REQUIRED_PROFILE)
        profile["z_token"] = "redacted"
        profile["a_secret"] = "redacted"
        profile["approval_channels"]["infoflow"] = {"z_token": "x", "a_secret": "y"}

        result = validate_profile(profile, check_paths=False)

        self.assertEqual(result["invalid"], sorted(set(result["invalid"])))

    def test_save_requires_confirmation_and_matching_hash_for_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "bgw.yaml"
            profile = _profile_with_existing_paths(root)
            rejected = save_profile(path, profile, None, confirmation=False)
            created = save_profile(path, profile, None, confirmation=True)
            stale = save_profile(path, profile, "0" * 64, confirmation=True)
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            overwritten = save_profile(path, profile, content_hash, confirmation=True)

        self.assertEqual(rejected["reason_code"], "PROFILE_CONFIRMATION_REQUIRED")
        self.assertEqual(created["reason_code"], "READY")
        self.assertEqual(stale["reason_code"], "PROFILE_CONFLICT")
        self.assertEqual(overwritten["reason_code"], "READY")

    def test_save_rejects_profile_with_unavailable_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            result = save_profile(
                Path(directory) / "projects" / "bgw.yaml",
                REQUIRED_PROFILE,
                None,
                confirmation=True,
            )

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertIn("business_repos[0].path", result["invalid"])

    def test_save_rechecks_hash_and_preserves_external_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "bgw.yaml"
            profile = _profile_with_existing_paths(root)
            save_profile(path, profile, None, confirmation=True)
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_text("external: update\n", encoding="utf-8")

            result = save_profile(path, profile, expected_hash, confirmation=True)

            self.assertEqual(result["reason_code"], "PROFILE_CONFLICT")
            self.assertEqual(path.read_text(encoding="utf-8"), "external: update\n")

    def test_concurrent_saves_allow_only_one_writer_for_a_confirmed_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "bgw.yaml"
            profile = _profile_with_existing_paths(root)
            save_profile(path, profile, None, confirmation=True)
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            profile["pipeline_profile"]["release_rule"] = "first-approval"
            competing_profile = copy.deepcopy(profile)
            competing_profile["pipeline_profile"]["release_rule"] = "alternate-approval"
            barrier = threading.Barrier(2)
            results = []

            def save(candidate: dict) -> None:
                barrier.wait()
                results.append(save_profile(path, candidate, expected_hash, confirmation=True)["reason_code"])

            first = threading.Thread(target=save, args=(profile,))
            second = threading.Thread(target=save, args=(competing_profile,))
            first.start()
            second.start()
            first.join()
            second.join()

        self.assertCountEqual(results, ["READY", "PROFILE_CONFLICT"])

    def test_save_interruption_leaves_existing_profile_and_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "bgw.yaml"
            profile = _profile_with_existing_paths(root)
            save_profile(path, profile, None, confirmation=True)
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            before = path.read_bytes()

            with patch("project_registry.os.replace", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    save_profile(path, profile, expected_hash, confirmation=True)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_missing_profile_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_profile(Path(directory) / "missing.yaml")

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(result["missing"], ["profile_file"])


def _profile_with_existing_paths(root: Path) -> dict:
    profile = copy.deepcopy(REQUIRED_PROFILE)
    business = root / "business"
    tests = root / "tests"
    language_skill = root / "language-skill"
    project_skill = root / "project-skill"
    for repository in (business, tests):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
    for skill in (language_skill, project_skill):
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
    profile["business_repos"][0]["path"] = str(business)
    profile["test_repo"]["path"] = str(tests)
    profile["language_skill"] = str(language_skill)
    profile["project_skill"] = str(project_skill)
    return profile


if __name__ == "__main__":
    unittest.main()
