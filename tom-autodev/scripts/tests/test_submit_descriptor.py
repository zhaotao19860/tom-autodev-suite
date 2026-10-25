import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import submit_descriptor


class SubmitDescriptorCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "dev@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "dev"], check=True)
        (self.repo / "file.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "file.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "CARD-1 initial\n\nChange-Id: Iccccccccccccccccccccccccccccccccccccccc"],
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_unpushable_head_is_refused(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "no trailer"],
            check=True,
        )
        with self.assertRaises(ValueError) as raised:
            submit_descriptor._commit_if_dirty(self.repo, "CARD-1 message")
        self.assertEqual(str(raised.exception), "CHANGE_ID_MISSING")

    def test_dirty_commit_adds_change_id_with_configured_identity(self):
        (self.repo / "file.txt").write_text("two\n", encoding="utf-8")
        revision = submit_descriptor._commit_if_dirty(self.repo, "CARD-1 reviewed")
        body = subprocess.check_output(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%B"], text=True
        )
        email = subprocess.check_output(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%ce"], text=True
        ).strip()
        self.assertTrue(revision)
        self.assertIn("Change-Id:", body)
        self.assertEqual(email, "dev@example.test")

    def test_matching_clean_worktree_uses_owned_sibling_not_hardcoded_path(self):
        sibling = Path(self.temp.name) / "run" / "T3" / "repo-id"
        sibling.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.repo), str(sibling)], check=True)
        subprocess.run(["git", "-C", str(sibling), "config", "user.email", "dev@example.test"], check=True)
        subprocess.run(["git", "-C", str(sibling), "config", "user.name", "dev"], check=True)
        revision = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        preferred = Path(self.temp.name) / "run" / "T9" / "repo-id"
        found = submit_descriptor._matching_clean_worktree(str(preferred), revision)
        self.assertEqual(Path(found).resolve(), sibling.resolve())
        self.assertNotIn("t3-rebuild", found)


class OwnedRowLookupTests(unittest.TestCase):
    def test_owned_row_resolves_symlinked_and_relative_paths(self):
        # Ownership rows are keyed by the fully resolved repo path; a lookup with a
        # symlinked root (macOS /var -> /private/var) or a trailing slash must still hit.
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory).resolve() / "repo"
            real.mkdir()
            link = Path(directory).resolve() / "link"
            link.symlink_to(real, target_is_directory=True)
            rows = {("T-1", str(real)): {"worktree_path": "/wt", "repo_path": str(real)}}

            self.assertIsNotNone(submit_descriptor.owned_row(rows, "T-1", str(link)))
            self.assertIsNotNone(submit_descriptor.owned_row(rows, "T-1", str(real) + "/"))
            self.assertIsNone(submit_descriptor.owned_row(rows, "T-2", str(real)))
            self.assertIsNone(submit_descriptor.owned_row(rows, "T-1", None))


class _Artifacts:
    def __init__(self, nodes):
        self.nodes = nodes

    def latest_phase(self, run_id, phase, task_id):
        return {"valid": True, "envelope": {"content": {"nodes": self.nodes}}}


class _Orchestrator:
    def __init__(self, nodes):
        self.artifacts = _Artifacts(nodes)


class UnchangedBaselineTests(unittest.TestCase):
    PROFILE = {"business_repos": [
        {"module": "A", "branch": "feature", "path": "/repo/a"},
        {"module": "B", "branch": "shared", "path": "/repo/b"},
    ]}

    def pins(self, nodes, profile=None, heads=None):
        heads = heads if heads is not None else {"shared": "b-head"}
        with unittest.mock.patch.object(
            submit_descriptor, "_remote_branch_head", side_effect=lambda path, branch: heads.get(branch)
        ):
            return submit_descriptor.unchanged_baselines(
                _Orchestrator(nodes), "run", profile or self.PROFILE
            )

    def test_untouched_repository_is_pinned_at_the_branch_head(self):
        self.assertEqual(
            self.pins([{"task_id": "T1", "business_module": "A"}]),
            [{"module": "B", "branch": "shared", "revision": "b-head"}],
        )

    def test_every_repository_touched_has_nothing_to_pin(self):
        nodes = [{"task_id": "T1", "business_module": "A"}, {"task_id": "T2", "business_module": "B"}]
        self.assertEqual(self.pins(nodes), [])

    def test_unnamed_task_repository_is_never_guessed(self):
        self.assertEqual(self.pins([{"task_id": "T1"}]), [])

    def test_single_repository_profile_has_nothing_to_pin(self):
        profile = {"business_repos": self.PROFILE["business_repos"][:1]}
        self.assertEqual(self.pins([{"task_id": "T1", "business_module": "A"}], profile), [])

    def test_unresolvable_head_is_reported_not_skipped(self):
        result = self.pins([{"task_id": "T1", "business_module": "A"}], heads={})
        self.assertEqual(result["reason_code"], "BASELINE_UNRESOLVED")
        self.assertEqual(result["module"], "B")

    def test_remote_head_reads_the_named_branch_only(self):
        with tempfile.TemporaryDirectory() as temp:
            remote = Path(temp) / "remote.git"
            work = Path(temp) / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            for key, value in (("user.email", "dev@example.test"), ("user.name", "dev")):
                subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
            subprocess.run(["git", "-C", str(work), "commit", "-q", "--allow-empty", "-m", "one"], check=True)
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/shared"], check=True)
            subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/shared-other"], check=True)
            head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()

            self.assertEqual(submit_descriptor._remote_branch_head(str(work), "shared"), head)
            self.assertIsNone(submit_descriptor._remote_branch_head(str(work), "missing"))
            self.assertIsNone(submit_descriptor._remote_branch_head("", "shared"))


if __name__ == "__main__":
    unittest.main()
