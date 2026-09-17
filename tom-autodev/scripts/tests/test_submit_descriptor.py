import subprocess
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
