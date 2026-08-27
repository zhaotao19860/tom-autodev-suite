import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile_discovery import discover_candidates


class FakeIcode:
    def discover(self, remote, revision):
        return [{"module": "engine", "remote": remote, "revision": revision}]


class FakeIpipe:
    def discover(self, remote, revision):
        return [{"pipeline_id": "pipeline-1", "remote": remote, "revision": revision}]


class ProfileDiscoveryTests(unittest.TestCase):
    def test_discovery_uses_git_identity_clients_and_explicit_test_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://example.test/repo.git"], check=True)

            result = discover_candidates(
                root,
                icode=FakeIcode(),
                ipipe=FakeIpipe(),
                mappings={
                    "test_repositories": [
                        {
                            "path": "/configured/tests",
                            "module": "tests",
                            "source_remote": "https://example.test/repo.git",
                            "source_revision": subprocess.run(
                                ["git", "-C", str(root), "rev-parse", "HEAD"],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout.strip(),
                        }
                    ]
                },
            )

        self.assertEqual(result["reason_code"], "READY")
        self.assertEqual(result["repository"]["remote"], "https://example.test/repo.git")
        self.assertEqual(result["test_repositories"][0]["path"], "/configured/tests")
        self.assertEqual(result["icode_candidates"][0]["module"], "engine")
        self.assertEqual(result["ipipe_candidates"][0]["pipeline_id"], "pipeline-1")

    def test_ambiguous_discovery_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://example.test/repo.git"], check=True)

            result = discover_candidates(
                root,
                icode=FakeIcode(),
                ipipe=FakeIpipe(),
                mappings={
                    "test_repositories": [
                        {"path": "/one", "source_remote": "https://example.test/repo.git"},
                        {"path": "/two", "source_remote": "https://example.test/repo.git"},
                    ]
                },
            )

        self.assertEqual(result["reason_code"], "PROFILE_CONFIRMATION_REQUIRED")

    def test_discovery_excludes_mappings_for_another_git_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _git_repository(Path(directory))

            result = discover_candidates(
                root,
                icode=FakeIcode(),
                ipipe=FakeIpipe(),
                mappings={"test_repositories": [{"path": "/other", "source_remote": "https://other.test/repo.git"}]},
            )

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(result["missing"], ["test_repositories"])

    def test_ambiguity_precedes_missing_candidates(self):
        class AmbiguousIcode:
            def discover(self, remote, revision):
                return [{"module": "one"}, {"module": "two"}]

        class MissingIpipe:
            def discover(self, remote, revision):
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = _git_repository(Path(directory))
            result = discover_candidates(
                root,
                icode=AmbiguousIcode(),
                ipipe=MissingIpipe(),
                mappings={"test_repositories": [{"path": "/tests", "source_remote": "https://example.test/repo.git"}]},
            )

        self.assertEqual(result["reason_code"], "PROFILE_CONFIRMATION_REQUIRED")

    def test_discovery_without_configured_test_repository_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _git_repository(Path(directory))

            result = discover_candidates(root, icode=FakeIcode(), ipipe=FakeIpipe())

        self.assertEqual(result["reason_code"], "PROJECT_NOT_READY")
        self.assertEqual(result["missing"], ["test_repositories"])


def _git_repository(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://example.test/repo.git"], check=True)
    return root


if __name__ == "__main__":
    unittest.main()
