import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lock_manager import LockManager
from recovery import Recovery
from state_store import StateStore
import workspace_manager
from workspace_manager import WorkspaceManager


class WorkspaceCreationTests(unittest.TestCase):
    def test_create_uses_exact_baseline_in_an_isolated_worktree_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            (repo / "tracked.txt").write_text("user change\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
            before_status = _git(repo, "status", "--porcelain")
            before_branch = _git(repo, "branch", "--show-current")
            manager = WorkspaceManager(root / "worktrees")

            created = manager.create(
                repo,
                "run-1",
                "task-1",
                {
                    "baseline_status": "READY",
                    "baseline_revision": revision,
                },
            )

            worktree = Path(created["worktree_path"])
            self.assertEqual(created["status"], "CREATED")
            self.assertTrue(created["owner_token"])
            self.assertNotEqual(worktree.resolve(), repo.resolve())
            self.assertEqual(_git(worktree, "rev-parse", "HEAD"), revision)
            self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "baseline\n")
            self.assertEqual(_git(repo, "status", "--porcelain"), before_status)
            self.assertEqual(_git(repo, "branch", "--show-current"), before_branch)
            self.assertEqual((repo / "untracked.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_create_rejects_unverified_or_changed_baseline_without_creating_a_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            manager = WorkspaceManager(root / "worktrees")

            unverified = manager.create(
                repo,
                "run-1",
                "task-unverified",
                {"baseline_revision": revision, "baseline_status": "BASELINE_UNVERIFIED"},
            )
            (repo / "tracked.txt").write_text("new baseline\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-m", "move baseline")
            changed = manager.create(
                repo,
                "run-1",
                "task-changed",
                {"baseline_revision": revision, "baseline_status": "READY"},
            )

            self.assertEqual(unverified["reason_code"], "BASELINE_UNVERIFIED")
            self.assertIsNone(unverified["worktree_path"])
            self.assertEqual(changed["reason_code"], "REVISION_MISMATCH")
            self.assertIsNone(changed["worktree_path"])
            self.assertFalse((root / "worktrees").exists())

    def test_create_rejects_a_worktree_root_inside_the_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            manager = WorkspaceManager(repo / "generated-worktrees")

            result = manager.create(
                repo,
                "run-1",
                "task-1",
                {"baseline_status": "READY", "baseline_revision": revision},
            )

            self.assertEqual(result["reason_code"], "WORKTREE_ROOT_INVALID")
            self.assertFalse((repo / "generated-worktrees").exists())

    def test_post_add_baseline_change_is_rejected_and_created_worktree_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            shim = _git_shim(root, "change-head-after-add")

            with patch.dict(os.environ, {"PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}):
                result = manager.create(
                    repo,
                    "run-1",
                    "task-race",
                    {"baseline_status": "READY", "baseline_revision": revision},
                )

            self.assertEqual(result["reason_code"], "REVISION_MISMATCH")
            self.assertIsNone(result["worktree_path"])
            self.assertNotEqual(_git(repo, "rev-parse", "HEAD"), revision)
            self.assertNotIn(str(worktree_root), _git(repo, "worktree", "list", "--porcelain"))
            self.assertFalse(worktree_root.exists())

    def test_failed_add_cleans_partial_registration_and_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            shim = _git_shim(root, "fail-after-add")

            with patch.dict(os.environ, {"PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}):
                result = manager.create(
                    repo,
                    "run-1",
                    "task-failure",
                    {"baseline_status": "READY", "baseline_revision": revision},
                )

            self.assertEqual(result["reason_code"], "WORKTREE_CREATE_FAILED")
            self.assertNotIn(str(worktree_root), _git(repo, "worktree", "list", "--porcelain"))
            self.assertFalse(worktree_root.exists())

    def test_remove_is_idempotent_and_prunes_git_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            manager = WorkspaceManager(root / "worktrees")
            created = manager.create(
                repo,
                "run-1",
                "task-1",
                {"baseline_status": "READY", "baseline_revision": revision},
            )

            owner_token = created["owner_token"]
            missing_token = manager.remove(repo, created["worktree_path"])
            wrong_token = manager.remove(repo, created["worktree_path"], "wrong-owner")
            removed = WorkspaceManager(root / "worktrees").remove(
                repo, created["worktree_path"], owner_token
            )
            repeated = manager.remove(repo, created["worktree_path"], owner_token)
            wrong_after_remove = manager.remove(
                repo, created["worktree_path"], "wrong-owner"
            )

            self.assertEqual(missing_token["reason_code"], "WORKTREE_OWNER_REQUIRED")
            self.assertEqual(wrong_token["reason_code"], "WORKTREE_OWNER_MISMATCH")
            self.assertEqual(removed["status"], "REMOVED")
            self.assertEqual(repeated["status"], "ALREADY_ABSENT")
            self.assertEqual(wrong_after_remove["reason_code"], "WORKTREE_OWNER_MISMATCH")
            self.assertFalse(Path(created["worktree_path"]).exists())
            self.assertNotIn(
                created["worktree_path"], _git(repo, "worktree", "list", "--porcelain")
            )

    def test_concurrent_create_reserves_one_owner_without_loser_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            shim = _git_shim(root, "pause-before-add")
            barrier = threading.Barrier(2)

            def create(_):
                barrier.wait()
                return WorkspaceManager(worktree_root).create(
                    repo,
                    "run-1",
                    "task-concurrent",
                    {"baseline_status": "READY", "baseline_revision": revision},
                )

            with patch.dict(os.environ, {"PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(create, range(2)))

            created = next(result for result in results if result["status"] == "CREATED")
            rejected = next(result for result in results if result["status"] == "BLOCKED")
            self.assertEqual(rejected["reason_code"], "WORKTREE_ALREADY_RESERVED")
            self.assertNotEqual(created["owner_token"], rejected.get("owner_token"))
            self.assertTrue(Path(created["worktree_path"]).is_dir())
            self.assertIn(
                created["worktree_path"], _git(repo, "worktree", "list", "--porcelain")
            )

    def test_foreign_registered_worktree_under_root_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            foreign = worktree_root / "foreign"
            foreign.parent.mkdir(parents=True)
            _git(repo, "worktree", "add", "--detach", str(foreign), revision)
            (foreign / "uncommitted.txt").write_text("must survive\n", encoding="utf-8")

            result = WorkspaceManager(worktree_root).remove(
                repo, foreign, "claimed-owner"
            )

            self.assertEqual(result["reason_code"], "WORKTREE_NOT_OWNED")
            self.assertTrue((foreign / "uncommitted.txt").is_file())
            self.assertIn(str(foreign), _git(repo, "worktree", "list", "--porcelain"))

    def test_reserved_receipt_without_add_retries_with_same_owner_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-before-add"
            manager = WorkspaceManager(worktree_root)

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaisesRegex(SystemExit, "crash"):
                        manager.create(
                            repo,
                            "run-crash",
                            "task-before-add",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            restarted = WorkspaceManager(worktree_root)
            recovered = restarted.reconcile(
                repo, "run-crash", "task-before-add", owner_token
            )
            created = restarted.create(
                repo,
                "run-crash",
                "task-before-add",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token=owner_token,
            )

            self.assertEqual(recovered["status"], "RECOVERY_REQUIRED")
            self.assertEqual(recovered["ownership_status"], "RESERVED")
            self.assertEqual(recovered["action"], "RETRY_CREATE")
            self.assertEqual(recovered["owner_token"], owner_token)
            self.assertEqual(recovered["baseline_revision"], revision)
            self.assertEqual(created["status"], "CREATED")
            self.assertEqual(created["owner_token"], owner_token)
            self.assertEqual(created["worktree_path"], recovered["worktree_path"])

    def test_reserved_partial_path_is_cleaned_only_by_matching_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-partial"

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaises(SystemExit):
                        WorkspaceManager(worktree_root).create(
                            repo,
                            "run-crash",
                            "task-partial",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            restarted = WorkspaceManager(worktree_root)
            receipt = restarted.reconcile(
                repo, "run-crash", "task-partial", owner_token
            )
            partial = Path(receipt["worktree_path"])
            partial.mkdir(parents=True)
            marker = partial / "partial.txt"
            marker.write_text("owned partial\n", encoding="utf-8")

            foreign = restarted.reconcile(
                repo, "run-crash", "task-partial", "foreign-owner"
            )
            self.assertEqual(foreign["reason_code"], "WORKTREE_OWNER_MISMATCH")
            self.assertNotIn("owner_token", foreign)
            self.assertTrue(marker.is_file())

            recovered = restarted.reconcile(
                repo, "run-crash", "task-partial", owner_token
            )

            self.assertEqual(recovered["ownership_status"], "RESERVED")
            self.assertEqual(recovered["action"], "RETRY_CREATE")
            self.assertFalse(partial.exists())

    def test_registered_reserved_receipt_reconciles_active_at_recorded_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-after-add"
            manager = WorkspaceManager(worktree_root)

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch.object(
                    manager, "_change_ownership_status", side_effect=SystemExit("crash")
                ):
                    with self.assertRaisesRegex(SystemExit, "crash"):
                        manager.create(
                            repo,
                            "run-crash",
                            "task-after-add",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            recovered = WorkspaceManager(worktree_root).reconcile(
                repo, "run-crash", "task-after-add", owner_token
            )
            repeated = WorkspaceManager(worktree_root).create(
                repo,
                "run-crash",
                "task-after-add",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token=owner_token,
            )

            self.assertEqual(recovered["status"], "RECONCILED")
            self.assertEqual(recovered["ownership_status"], "ACTIVE")
            self.assertEqual(recovered["action"], "NONE")
            self.assertEqual(recovered["owner_token"], owner_token)
            self.assertEqual(_git(Path(recovered["worktree_path"]), "rev-parse", "HEAD"), revision)
            self.assertEqual(repeated["status"], "CREATED")
            self.assertEqual(repeated["worktree_path"], recovered["worktree_path"])
            self.assertEqual(repeated["owner_token"], owner_token)

    def test_registration_query_failure_blocks_recovery_without_removing_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-query-failure"
            manager = WorkspaceManager(worktree_root)

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch.object(
                    manager, "_change_ownership_status", side_effect=SystemExit("crash")
                ):
                    with self.assertRaisesRegex(SystemExit, "crash"):
                        manager.create(
                            repo,
                            "run-query",
                            "task-query",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            worktree = _secondary_worktree(repo)
            marker = worktree / "uncommitted.txt"
            marker.write_text("must survive\n", encoding="utf-8")
            real_git = workspace_manager._git

            def fail_worktree_list(repo_path, *args):
                if args == ("worktree", "list", "--porcelain"):
                    return None
                return real_git(repo_path, *args)

            with patch("workspace_manager._git", side_effect=fail_worktree_list):
                blocked = WorkspaceManager(worktree_root).reconcile(
                    repo, "run-query", "task-query", owner_token
                )

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(
                blocked["reason_code"], "WORKTREE_REGISTRATION_QUERY_FAILED"
            )
            self.assertEqual(blocked["ownership_status"], "RESERVED")
            self.assertEqual(blocked["action"], "RETRY_RECONCILE")
            self.assertEqual(blocked["owner_token"], owner_token)
            self.assertTrue(marker.is_file())
            self.assertIn(str(worktree), _git(repo, "worktree", "list", "--porcelain"))

    def test_cleanup_states_leave_registered_wrong_revision_untouched(self):
        for recovery_status in ("FAILED", "CLEANING", "REMOVING"):
            with self.subTest(recovery_status=recovery_status):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    repo = _git_repo(root)
                    baseline_revision = _git(repo, "rev-parse", "HEAD")
                    worktree_root = root / "worktrees"
                    manager = WorkspaceManager(worktree_root)
                    created = manager.create(
                        repo,
                        "run-wrong-head",
                        f"task-{recovery_status.lower()}",
                        {
                            "baseline_status": "READY",
                            "baseline_revision": baseline_revision,
                        },
                    )
                    worktree = Path(created["worktree_path"])
                    (repo / "tracked.txt").write_text("moved\n", encoding="utf-8")
                    _git(repo, "add", "tracked.txt")
                    _git(repo, "commit", "-m", "move source")
                    different_revision = _git(repo, "rev-parse", "HEAD")
                    _git(worktree, "checkout", "--detach", different_revision)
                    marker = worktree / "uncommitted.txt"
                    marker.write_text("must survive\n", encoding="utf-8")
                    database = root / ".worktrees.tom-autodev-ownership.sqlite"
                    with sqlite3.connect(database) as connection:
                        connection.execute(
                            "UPDATE worktree_ownership SET status = ? WHERE worktree_path = ?",
                            (recovery_status, str(worktree)),
                        )

                    blocked = WorkspaceManager(worktree_root).reconcile(
                        repo,
                        "run-wrong-head",
                        f"task-{recovery_status.lower()}",
                        created["owner_token"],
                    )

                    self.assertEqual(blocked["status"], "BLOCKED")
                    self.assertEqual(
                        blocked["reason_code"], "WORKTREE_REVISION_MISMATCH"
                    )
                    self.assertEqual(blocked["ownership_status"], recovery_status)
                    self.assertEqual(blocked["action"], "STOP")
                    self.assertEqual(blocked["owner_token"], created["owner_token"])
                    self.assertTrue(marker.is_file())
                    self.assertEqual(
                        _git(worktree, "rev-parse", "HEAD"), different_revision
                    )
                    self.assertIn(
                        str(worktree), _git(repo, "worktree", "list", "--porcelain")
                    )

    def test_unregistered_foreign_checkout_at_reserved_path_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-foreign-checkout"

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaisesRegex(SystemExit, "crash"):
                        WorkspaceManager(worktree_root).create(
                            repo,
                            "run-foreign",
                            "task-foreign",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            manager = WorkspaceManager(worktree_root)
            receipt = manager.reconcile(
                repo, "run-foreign", "task-foreign", owner_token
            )
            foreign = Path(receipt["worktree_path"])
            foreign.mkdir(parents=True)
            _git(foreign, "init")
            _git(foreign, "config", "user.email", "test@example.com")
            _git(foreign, "config", "user.name", "Test User")
            marker = foreign / "foreign.txt"
            marker.write_text("must survive\n", encoding="utf-8")
            _git(foreign, "add", "foreign.txt")
            _git(foreign, "commit", "-m", "foreign checkout")

            blocked = WorkspaceManager(worktree_root).reconcile(
                repo, "run-foreign", "task-foreign", owner_token
            )

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["reason_code"], "WORKTREE_CHECKOUT_PRESENT")
            self.assertEqual(blocked["ownership_status"], "RESERVED")
            self.assertEqual(blocked["action"], "STOP")
            self.assertEqual(blocked["owner_token"], owner_token)
            self.assertEqual(_git(foreign, "rev-parse", "--show-toplevel"), str(foreign))
            self.assertTrue(marker.is_file())
            self.assertNotIn(
                str(foreign), _git(repo, "worktree", "list", "--porcelain")
            )

    def test_registered_path_replaced_by_same_revision_foreign_checkout_is_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            created = manager.create(
                repo,
                "run-replaced",
                "task-replaced",
                {"baseline_status": "READY", "baseline_revision": revision},
            )
            worktree = Path(created["worktree_path"])
            shutil.rmtree(worktree)
            _git(root, "clone", "--no-local", str(repo), str(worktree))
            marker = worktree / "uncommitted.txt"
            marker.write_text("must survive\n", encoding="utf-8")

            blocked = WorkspaceManager(worktree_root).remove(
                repo, worktree, created["owner_token"]
            )

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(
                blocked["reason_code"], "WORKTREE_REPOSITORY_MISMATCH"
            )
            self.assertEqual(blocked["ownership_status"], "ACTIVE")
            self.assertEqual(blocked["action"], "STOP")
            self.assertEqual(blocked["owner_token"], created["owner_token"])
            self.assertEqual(_git(worktree, "rev-parse", "HEAD"), revision)
            self.assertTrue(marker.is_file())
            self.assertIn(str(worktree), _git(repo, "worktree", "list", "--porcelain"))

    def test_reserved_recovery_rechecks_source_head_before_becoming_active(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            baseline_revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-source-moved"
            manager = WorkspaceManager(worktree_root)

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch.object(
                    manager, "_change_ownership_status", side_effect=SystemExit("crash")
                ):
                    with self.assertRaisesRegex(SystemExit, "crash"):
                        manager.create(
                            repo,
                            "run-source-moved",
                            "task-source-moved",
                            {
                                "baseline_status": "READY",
                                "baseline_revision": baseline_revision,
                            },
                        )

            worktree = _secondary_worktree(repo)
            (repo / "tracked.txt").write_text("source moved\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-m", "move source after add")
            marker = worktree / "uncommitted.txt"
            marker.write_text("must survive\n", encoding="utf-8")

            blocked = WorkspaceManager(worktree_root).reconcile(
                repo, "run-source-moved", "task-source-moved", owner_token
            )

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["reason_code"], "REVISION_MISMATCH")
            self.assertEqual(blocked["ownership_status"], "RESERVED")
            self.assertEqual(blocked["action"], "STOP")
            self.assertEqual(blocked["owner_token"], owner_token)
            self.assertEqual(_git(worktree, "rev-parse", "HEAD"), baseline_revision)
            self.assertTrue(marker.is_file())
            self.assertIn(str(worktree), _git(repo, "worktree", "list", "--porcelain"))

    def test_reserved_registration_at_another_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            baseline_revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "owner-mismatch"

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaises(SystemExit):
                        WorkspaceManager(worktree_root).create(
                            repo,
                            "run-crash",
                            "task-mismatch",
                            {
                                "baseline_status": "READY",
                                "baseline_revision": baseline_revision,
                            },
                        )

            manager = WorkspaceManager(worktree_root)
            receipt = manager.reconcile(
                repo, "run-crash", "task-mismatch", owner_token
            )
            (repo / "tracked.txt").write_text("different\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-m", "different revision")
            different_revision = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "--detach", baseline_revision)
            worktree = Path(receipt["worktree_path"])
            _git(repo, "worktree", "add", "--detach", str(worktree), different_revision)
            (worktree / "foreign.txt").write_text("keep\n", encoding="utf-8")

            blocked = WorkspaceManager(worktree_root).reconcile(
                repo, "run-crash", "task-mismatch", owner_token
            )

            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["reason_code"], "WORKTREE_REVISION_MISMATCH")
            self.assertEqual(blocked["ownership_status"], "RESERVED")
            self.assertTrue((worktree / "foreign.txt").is_file())
            self.assertIn(str(worktree), _git(repo, "worktree", "list", "--porcelain"))

    def test_failed_receipt_retries_only_with_matching_owner_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            shim = _git_shim(root, "fail-after-add")

            with patch.dict(os.environ, {"PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}):
                failed = WorkspaceManager(worktree_root).create(
                    repo,
                    "run-retry",
                    "task-retry",
                    {"baseline_status": "READY", "baseline_revision": revision},
                )

            owner_token = failed["owner_token"]
            restarted = WorkspaceManager(worktree_root)
            foreign = restarted.create(
                repo,
                "run-retry",
                "task-retry",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token="foreign-owner",
            )
            recovered = restarted.reconcile(
                repo, "run-retry", "task-retry", owner_token
            )
            retried = restarted.create(
                repo,
                "run-retry",
                "task-retry",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token=owner_token,
            )

            self.assertEqual(foreign["reason_code"], "WORKTREE_OWNER_MISMATCH")
            self.assertEqual(recovered["ownership_status"], "FAILED")
            self.assertEqual(recovered["action"], "RETRY_CREATE")
            self.assertEqual(retried["status"], "CREATED")
            self.assertEqual(retried["owner_token"], owner_token)

    def test_removing_receipt_continues_registered_removal_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            created = manager.create(
                repo,
                "run-remove",
                "task-before-remove",
                {"baseline_status": "READY", "baseline_revision": revision},
            )
            real_git_result = workspace_manager._git_result

            def crash_before_git_remove(repo_path, *args):
                if args[:2] == ("worktree", "remove"):
                    raise SystemExit("crash")
                return real_git_result(repo_path, *args)

            with patch("workspace_manager._git_result", side_effect=crash_before_git_remove):
                with self.assertRaisesRegex(SystemExit, "crash"):
                    manager.remove(repo, created["worktree_path"], created["owner_token"])

            removed = WorkspaceManager(worktree_root).remove(
                repo, created["worktree_path"], created["owner_token"]
            )

            self.assertEqual(removed["status"], "REMOVED")
            self.assertEqual(removed["ownership_status"], "REMOVED")
            self.assertEqual(removed["owner_token"], created["owner_token"])
            self.assertFalse(Path(created["worktree_path"]).exists())

    def test_removing_receipt_finalizes_absence_after_git_remove_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            created = manager.create(
                repo,
                "run-remove",
                "task-after-remove",
                {"baseline_status": "READY", "baseline_revision": revision},
            )
            original_change = manager._change_ownership_status

            def crash_before_removed(*args):
                if args[-2:] == ("REMOVING", "REMOVED"):
                    raise SystemExit("crash")
                return original_change(*args)

            with patch.object(
                manager, "_change_ownership_status", side_effect=crash_before_removed
            ):
                with self.assertRaisesRegex(SystemExit, "crash"):
                    manager.remove(repo, created["worktree_path"], created["owner_token"])

            restarted = WorkspaceManager(worktree_root)
            foreign = restarted.reconcile(
                repo, "run-remove", "task-after-remove", "foreign-owner"
            )
            recovered = restarted.reconcile(
                repo, "run-remove", "task-after-remove", created["owner_token"]
            )

            self.assertEqual(foreign["reason_code"], "WORKTREE_OWNER_MISMATCH")
            self.assertEqual(recovered["status"], "ALREADY_ABSENT")
            self.assertEqual(recovered["ownership_status"], "REMOVED")
            self.assertEqual(recovered["owner_token"], created["owner_token"])
            self.assertEqual(recovered["action"], "NONE")
            self.assertFalse(Path(created["worktree_path"]).exists())

    def test_removed_tombstone_retries_same_owner_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            created = manager.create(
                repo,
                "run-reuse",
                "task-reuse",
                {"baseline_status": "READY", "baseline_revision": revision},
            )
            manager.remove(repo, created["worktree_path"], created["owner_token"])

            recreated = WorkspaceManager(worktree_root).create(
                repo,
                "run-reuse",
                "task-reuse",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token=created["owner_token"],
            )
            database = root / ".worktrees.tom-autodev-ownership.sqlite"
            with sqlite3.connect(database) as connection:
                history = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT status
                        FROM worktree_ownership_history
                        WHERE worktree_path = ? AND owner_token = ?
                        ORDER BY sequence
                        """,
                        (created["worktree_path"], created["owner_token"]),
                    )
                ]

            self.assertEqual(recreated["status"], "CREATED")
            self.assertEqual(recreated["owner_token"], created["owner_token"])
            self.assertEqual(
                history,
                ["RESERVED", "ACTIVE", "REMOVING", "REMOVED", "RESERVED", "ACTIVE"],
            )

    def test_legacy_active_receipt_without_revision_fails_closed_on_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            manager = WorkspaceManager(worktree_root)
            created = manager.create(
                repo,
                "run-legacy",
                "task-remove",
                {"baseline_status": "READY", "baseline_revision": revision},
            )
            database = root / ".worktrees.tom-autodev-ownership.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE worktree_ownership SET baseline_revision = NULL"
                )

            removed = WorkspaceManager(worktree_root).remove(
                repo, created["worktree_path"], created["owner_token"]
            )

            self.assertEqual(removed["status"], "BLOCKED")
            self.assertEqual(removed["reason_code"], "WORKTREE_BASELINE_UNKNOWN")
            self.assertEqual(removed["ownership_status"], "ACTIVE")
            self.assertEqual(removed["action"], "STOP")
            self.assertEqual(removed["owner_token"], created["owner_token"])
            self.assertTrue(Path(created["worktree_path"]).is_dir())
            self.assertIn(
                created["worktree_path"], _git(repo, "worktree", "list", "--porcelain")
            )

    def test_legacy_reserved_receipt_backfills_validated_revision_on_reentry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "legacy-reserved-owner"

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaises(SystemExit):
                        WorkspaceManager(worktree_root).create(
                            repo,
                            "run-legacy",
                            "task-reserved",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )
            database = root / ".worktrees.tom-autodev-ownership.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE worktree_ownership SET baseline_revision = NULL"
                )

            recovered = WorkspaceManager(worktree_root).create(
                repo,
                "run-legacy",
                "task-reserved",
                {"baseline_status": "READY", "baseline_revision": revision},
                owner_token=owner_token,
            )

            self.assertEqual(recovered["status"], "CREATED")
            self.assertEqual(recovered["baseline_revision"], revision)
            self.assertEqual(recovered["owner_token"], owner_token)

    def test_concurrent_same_owner_reentry_serializes_add_and_preserves_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _git_repo(root)
            revision = _git(repo, "rev-parse", "HEAD")
            worktree_root = root / "worktrees"
            owner_token = "same-recovery-owner"

            with patch("workspace_manager.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = owner_token
                with patch("workspace_manager._git_result", side_effect=SystemExit("crash")):
                    with self.assertRaises(SystemExit):
                        WorkspaceManager(worktree_root).create(
                            repo,
                            "run-recovery-race",
                            "task-recovery-race",
                            {"baseline_status": "READY", "baseline_revision": revision},
                        )

            shim = _git_shim(root, "race-same-owner-reentry")
            barrier = threading.Barrier(2)

            def recover(_):
                barrier.wait()
                return WorkspaceManager(worktree_root).create(
                    repo,
                    "run-recovery-race",
                    "task-recovery-race",
                    {"baseline_status": "READY", "baseline_revision": revision},
                    owner_token=owner_token,
                )

            with patch.dict(os.environ, {"PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}"}):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(recover, range(2)))

            self.assertEqual([result["status"] for result in results], ["CREATED", "CREATED"])
            self.assertEqual(len({result["worktree_path"] for result in results}), 1)
            worktree = Path(results[0]["worktree_path"])
            self.assertTrue(worktree.is_dir())
            self.assertEqual(_git(worktree, "rev-parse", "HEAD"), revision)
            self.assertIn(str(worktree), _git(repo, "worktree", "list", "--porcelain"))


class LockManagerTests(unittest.TestCase):
    def test_acquisition_is_atomic_and_an_active_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            barrier = threading.Barrier(2)

            def acquire(token):
                barrier.wait()
                return LockManager(database).acquire("repo:task", token, 60)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(acquire, ["owner-a", "owner-b"]))

            self.assertEqual(sum(result["acquired"] for result in results), 1)
            rejected = next(result for result in results if not result["acquired"])
            self.assertEqual(rejected["reason_code"], "LOCK_ACTIVE")
            self.assertFalse(rejected["takeover"])

    def test_stale_takeover_requires_expired_heartbeat_and_dead_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            clock = MutableClock(datetime(2026, 8, 10, tzinfo=timezone.utc))
            dead_owner = LockManager(database, pid=2_000_000_000, clock=clock)
            first = dead_owner.acquire("repo:task-a", "dead-owner", 30)
            live_owner = LockManager(database, pid=os.getpid(), clock=clock)
            live_owner.acquire("repo:task-b", "live-owner", 30)

            clock.advance(31)
            takeover = LockManager(database, pid=os.getpid(), clock=clock).acquire(
                "repo:task-a", "new-owner", 30
            )
            live_rejected = LockManager(database, pid=os.getpid(), clock=clock).acquire(
                "repo:task-b", "other-owner", 30
            )

            self.assertTrue(first["acquired"])
            self.assertTrue(takeover["acquired"])
            self.assertTrue(takeover["takeover"])
            self.assertEqual(takeover["previous_owner_token"], "dead-owner")
            self.assertFalse(live_rejected["acquired"])
            self.assertEqual(live_rejected["reason_code"], "LOCK_ACTIVE")

    def test_heartbeat_extends_ownership_and_wrong_owner_cannot_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            clock = MutableClock(datetime(2026, 8, 10, tzinfo=timezone.utc))
            owner = LockManager(database, pid=2_000_000_000, clock=clock)
            owner.acquire("repo:task", "owner-a", 30)
            clock.advance(20)
            heartbeat = owner.heartbeat("repo:task", "owner-a")
            clock.advance(20)

            rejected = LockManager(database, pid=os.getpid(), clock=clock).acquire(
                "repo:task", "owner-b", 30
            )

            self.assertEqual(heartbeat["status"], "HEARTBEAT_RECORDED")
            self.assertFalse(rejected["acquired"])
            with self.assertRaisesRegex(ValueError, "LOCK_OWNER_MISMATCH"):
                owner.heartbeat("repo:task", "wrong-owner")


class RecoveryTests(unittest.TestCase):
    def test_resume_returns_query_actions_for_every_uncertain_intent_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            state = StateStore(database)
            checkpoint = state.transition("run-1", "IMPLEMENT", {"task_id": "task-1"})
            second = state.intent("run-1", "trigger-pipeline", "pipeline:2", {"pipeline": "p2"})
            first = state.intent("run-1", "submit-cr", "submit:1", {"task": "task-1"})

            resumed = Recovery(database).resume("run-1")

            self.assertEqual(resumed["checkpoint"], checkpoint)
            self.assertEqual(resumed["status"], "QUERY_REQUIRED")
            self.assertFalse(resumed["retry_allowed"])
            self.assertEqual(
                resumed["actions"],
                [
                    {
                        "action": "QUERY_EXTERNAL_STATUS",
                        "reason_code": "QUERY_REQUIRED",
                        "intent_id": second["intent_id"],
                        "operation": "trigger-pipeline",
                        "idempotency_key": "pipeline:2",
                    },
                    {
                        "action": "QUERY_EXTERNAL_STATUS",
                        "reason_code": "QUERY_REQUIRED",
                        "intent_id": first["intent_id"],
                        "operation": "submit-cr",
                        "idempotency_key": "submit:1",
                    },
                ],
            )

    def test_resume_exposes_locks_and_incomplete_handoffs_in_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            clock = MutableClock(datetime(2026, 8, 10, tzinfo=timezone.utc))
            state = StateStore(database)
            state.transition("run-1", "PLAN", {})
            state.record_handoff("run-1", "handoff-z", {"phase": "PLAN"})
            state.record_handoff("run-1", "handoff-a", {"phase": "SPEC"})
            locks = LockManager(database, pid=2_000_000_000, clock=clock)
            locks.acquire("z-lock", "z-owner", 10)
            LockManager(database, pid=os.getpid(), clock=clock).acquire(
                "a-lock", "a-owner", 10
            )
            clock.advance(11)

            resumed = Recovery(database, clock=clock).resume("run-1")

            self.assertEqual([lock["key"] for lock in resumed["active_locks"]], ["a-lock"])
            self.assertEqual([lock["key"] for lock in resumed["stale_locks"]], ["z-lock"])
            self.assertEqual(
                [handoff["handoff_id"] for handoff in resumed["incomplete_handoffs"]],
                ["handoff-a", "handoff-z"],
            )


class MutableClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


def _git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _secondary_worktree(repo: Path) -> Path:
    paths = [
        Path(line.removeprefix("worktree "))
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    return next(path for path in paths if path.resolve() != repo.resolve())


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def _git_shim(root: Path, mode: str) -> Path:
    real_git = shutil.which("git")
    if real_git is None:
        raise RuntimeError("git is required for workspace tests")
    bin_dir = root / f"bin-{mode}"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    before = ""
    action = ""
    if mode == "pause-before-add":
        before = """
if [ "$1" = "worktree" ] && [ "$2" = "add" ]; then
  sleep 0.2
fi
"""
    elif mode == "race-same-owner-reentry":
        gate = root / "same-owner-add-gate"
        added = root / "same-owner-added"
        before = f"""
if [ "$1" = "worktree" ] && [ "$2" = "add" ]; then
  if mkdir {shlex.quote(str(gate))} 2>/dev/null; then
    {shlex.quote(real_git)} "$@"
    status=$?
    touch {shlex.quote(str(added))}
    sleep 0.3
    exit $status
  fi
  while [ ! -f {shlex.quote(str(added))} ]; do
    sleep 0.01
  done
  exit 1
fi
"""
    if mode == "change-head-after-add":
        action = f"""
if [ "$1" = "worktree" ] && [ "$2" = "add" ]; then
  printf 'concurrent change\\n' > "$PWD/concurrent.txt"
  {shlex.quote(real_git)} add concurrent.txt
  {shlex.quote(real_git)} commit -q -m concurrent-change
fi
"""
    elif mode == "fail-after-add":
        action = """
if [ "$1" = "worktree" ] && [ "$2" = "add" ]; then
  exit 1
fi
"""
    shim.write_text(
        f"""#!/bin/sh
{before}
{shlex.quote(real_git)} "$@"
status=$?
if [ $status -ne 0 ]; then
  exit $status
fi
{action}
exit 0
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


if __name__ == "__main__":
    unittest.main()
