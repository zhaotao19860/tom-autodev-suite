"""Guard: an owned worktree must carry at most one commit above its baseline.

iCode/Gerrit rejects a push with more than one commit per CR (HCK_F507). The
IMPLEMENT producer path (prepare_change_set) must detect that early, before the
change set's revisions and the G5/G7 approvals bind to a multi-commit HEAD.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prepare_change_set import _commits_above_baseline


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def _commit(root, name):
    (Path(root) / name).write_text(name + "\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", name)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


class SingleCommitGuardTests(unittest.TestCase):
    def _repo(self, directory):
        _git(directory, "init", "-q")
        _git(directory, "config", "user.name", "tester")
        _git(directory, "config", "user.email", "tester@example.test")
        return _commit(directory, "baseline")

    def test_zero_commits_above_baseline_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._repo(directory)
            self.assertEqual(_commits_above_baseline(Path(directory), baseline), 0)

    def test_single_commit_above_baseline_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._repo(directory)
            _commit(directory, "change")
            self.assertEqual(_commits_above_baseline(Path(directory), baseline), 1)

    def test_two_commits_above_baseline_is_the_rejected_case(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._repo(directory)
            _commit(directory, "fix")
            _commit(directory, "tests")
            # This is exactly the HCK_F507 shape prepare_change_set now refuses.
            self.assertGreater(_commits_above_baseline(Path(directory), baseline), 1)


if __name__ == "__main__":
    unittest.main()
