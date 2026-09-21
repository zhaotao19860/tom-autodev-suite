"""Exercise the source-only collector on isolated Git repositories, not project code."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_scope.py"


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Scope Test")
        self.git("config", "user.email", "scope@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / "no-hooks"))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args]).decode().strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def collector(self):
        self.assertTrue(SCRIPT.is_file(), "missing deterministic source-scope collector")
        spec = importlib.util.spec_from_file_location("collect_scope", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_uses_pinned_commits_and_preserves_dirty_workspace(self):
        (self.repo / "value.c").write_text("old\n")
        base = self.commit("base")
        (self.repo / "value.c").write_text("candidate\n")
        target = self.commit("candidate")
        (self.repo / "value.c").write_text("later commit\n")
        head = self.commit("later")
        (self.repo / "value.c").write_text("user work\n")
        (self.repo / "untracked.txt").write_text("user data\n")
        before = self.git("status", "--porcelain=v1")
        result = self.collector().collect_scope(self.repo, base, target)
        self.assertEqual(result["candidate_revision"], target)
        self.assertEqual(result["baseline_revision"], base)
        self.assertTrue(result["worktree_changes_ignored"])
        self.assertEqual(result["files"], [{
            "path": "value.c", "status": "M", "old_mode": "100644",
            "new_mode": "100644", "added": 1, "deleted": 1, "binary": False,
        }])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertEqual((self.repo / "value.c").read_text(), "user work\n")

    def test_special_paths_binary_deletions_and_renames_are_not_lost(self):
        (self.repo / "old.npl").write_text("same\n")
        (self.repo / "gone.txt").write_text("remove\n")
        base = self.commit("base")
        (self.repo / "old.npl").rename(self.repo / "new.npl")
        (self.repo / "gone.txt").unlink()
        odd_name = "空 格\tline\nname.c"
        (self.repo / odd_name).write_text("new\n")
        (self.repo / "asset.bin").write_bytes(b"\x00\x01\xff")
        target = self.commit("changes")
        result = self.collector().collect_scope(self.repo, base, target)
        entries = {entry["path"]: entry for entry in result["files"]}
        self.assertEqual(set(entries), {"old.npl", "new.npl", "gone.txt", odd_name, "asset.bin"})
        self.assertEqual(entries["old.npl"]["status"], "D")
        self.assertEqual(entries["new.npl"]["status"], "A")
        self.assertEqual(entries["gone.txt"]["deleted"], 1)
        self.assertEqual(entries[odd_name]["added"], 1)
        self.assertTrue(entries["asset.bin"]["binary"])
        self.assertIsNone(entries["asset.bin"]["added"])

    def test_disables_external_diff_and_textconv(self):
        marker = self.repo / "ran-driver"
        driver = self.repo / "diff-driver.sh"
        driver.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nprintf bad\\n\n')
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text("*.c diff=custom\n")
        (self.repo / "value.c").write_text("old\n")
        base = self.commit("base")
        (self.repo / "value.c").write_text("new\n")
        target = self.commit("new")
        self.git("config", "diff.external", str(driver))
        self.git("config", "diff.custom.command", str(driver))
        self.git("config", "diff.custom.textconv", str(driver))
        result = self.collector().collect_scope(self.repo, base, target)
        self.assertEqual(result["files"][0]["added"], 1)
        self.assertFalse(marker.exists())

    def test_rejects_moving_refs_and_missing_objects(self):
        (self.repo / "value.c").write_text("x\n")
        rev = self.commit("base")
        module = self.collector()
        for ref in ("HEAD", "--help", "a" * 40):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    module.collect_scope(self.repo, rev, ref)

    def test_does_not_execute_clean_filter_when_worktree_differs(self):
        marker = self.repo / "ran-clean-filter"
        driver = self.repo / "clean-filter.sh"
        driver.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\ncat\n')
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text("*.c filter=custom\n")
        (self.repo / "value.c").write_text("old\n")
        base = self.commit("base")
        (self.repo / "value.c").write_text("new\n")
        target = self.commit("candidate")
        self.git("config", "filter.custom.clean", str(driver))
        # Same-size modification forces Git status to compare filtered contents.
        (self.repo / "value.c").write_text("bad\n")
        result = self.collector().collect_scope(self.repo, base, target)
        self.assertEqual(result["files"][0]["added"], 1)
        self.assertFalse(marker.exists(), "scope collection ran the repository's clean filter")

    def test_empty_diff_and_mode_only_changes_remain_visible(self):
        f = self.repo / "script.sh"
        f.write_text("echo ok\n")
        base = self.commit("base")
        module = self.collector()
        self.assertEqual(module.collect_scope(self.repo, base, base)["files"], [])
        self.git("update-index", "--chmod=+x", "script.sh")
        self.git("commit", "-qm", "mode")
        target = self.git("rev-parse", "HEAD")
        result = module.collect_scope(self.repo, base, target)
        self.assertEqual(result["files"][0]["new_mode"], "100755")
        self.assertEqual(result["files"][0]["added"], 0)


if __name__ == "__main__":
    unittest.main()
