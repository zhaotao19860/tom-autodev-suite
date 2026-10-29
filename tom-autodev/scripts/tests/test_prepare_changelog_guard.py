"""Guard: every repository a change set modifies carries its own change-log entry."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prepare_change_set import _missing_changelog


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD")


class ChangelogGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.name", "tester")
        _git(self.root, "config", "user.email", "tester@example.test")
        self.baseline = _commit(self.root, {"README.md": "x\n"})

    def tearDown(self):
        self.temp.cleanup()

    def test_a_change_without_a_changelog_entry_is_refused(self):
        head = _commit(self.root, {"README.md": "y\n"})
        self.assertTrue(_missing_changelog(self.root, self.baseline, head))

    def test_a_change_with_the_root_changelog_passes(self):
        head = _commit(self.root, {"README.md": "y\n", "CHANGELOG.md": "entry\n"})
        self.assertFalse(_missing_changelog(self.root, self.baseline, head))

    def test_the_lowercase_bgw_changelog_counts(self):
        head = _commit(self.root, {"src/a.c": "int a;\n", "changelog": "2.1.11\n"})
        self.assertFalse(_missing_changelog(self.root, self.baseline, head))

    def test_a_nested_changelog_does_not_count(self):
        head = _commit(self.root, {"docs/CHANGELOG.md": "entry\n"})
        self.assertTrue(_missing_changelog(self.root, self.baseline, head))

    def test_an_untouched_repository_needs_no_entry(self):
        self.assertFalse(_missing_changelog(self.root, self.baseline, self.baseline))


if __name__ == "__main__":
    unittest.main()
