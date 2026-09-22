"""WF-05 evidence through real worker entry points, with temporary Git and SQLite.

The host supplies the shared LockManager. These tests do not establish real Comate
host integration or make concurrent locks=None calls a supported mode.
"""

import sqlite3
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_fake_e2e as e2e
import worker_driver
import workspace_manager
from lock_manager import LockManager
from orchestrator import Orchestrator
from submit_descriptor import _ownership_rows


class WorkerConcurrencyContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = e2e.FakeE2ETests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.owner = self.fixture.orchestrator

    def run_at_workspace(self, card):
        return self.fixture._run_to_workspace(card, "bgw", "I15ClP2KW4ZGAK")

    @staticmethod
    def git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout

    def drive(self, owner, run_id, knowledge, token, *, locked=True):
        return worker_driver.advance(
            owner, run_id, knowledge_sync=knowledge,
            locks=owner.recovery.locks if locked else None, owner_token=token,
        )

    def ownership(self, owner, run_id):
        rows = _ownership_rows(owner, run_id)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({row["status"] for row in rows.values()}, {"ACTIVE"})
        return rows

    def lease_history(self, key):
        with sqlite3.connect(self.owner.state.database_path) as connection:
            return [row[0] for row in connection.execute(
                "SELECT kind FROM heartbeats WHERE lock_key = ? ORDER BY sequence", (key,))]

    def test_two_runs_share_source_repositories_without_crossing_workspace_ownership(self):
        first, knowledge1 = self.run_at_workspace("BGW-9501")
        second, knowledge2 = self.run_at_workspace("BGW-9502")
        owners = [self.owner, Orchestrator(self.fixture.root)]
        runs = [first, second]
        knowledge = [knowledge1, knowledge2]
        repos = [self.fixture.root / "bgw-business", self.fixture.root / "bgw-tests"]
        before = {}
        for repo in repos:
            (repo / "README.md").write_text("uncommitted user bytes\n", encoding="utf-8")
            (repo / "user-untracked.txt").write_text("keep untracked\n", encoding="utf-8")
            before[str(repo)] = {
                "head": self.git(repo, "rev-parse", "HEAD"),
                "branch": self.git(repo, "branch", "--show-current"),
                "status": self.git(repo, "status", "--porcelain"),
                "tracked": (repo / "README.md").read_bytes(),
                "untracked": (repo / "user-untracked.txt").read_bytes(),
            }

        real_lock = workspace_manager._ownership_operation_lock
        entrants = threading.Barrier(2)
        local = threading.local()
        observed = {"active": 0, "maximum_active": 0, "entrants": 0}
        monitor = threading.Lock()

        @contextmanager
        def contended_operation_lock(root):
            # Both real workers reach the same resource lock before either is allowed
            # to enter it; this is not just a direct LockManager unit test.
            if not getattr(local, "joined", False):
                local.joined = True
                with monitor:
                    observed["entrants"] += 1
                entrants.wait(timeout=10)
            with real_lock(root):
                with monitor:
                    observed["active"] += 1
                    observed["maximum_active"] = max(observed["maximum_active"], observed["active"])
                try:
                    yield
                finally:
                    with monitor:
                        observed["active"] -= 1

        with patch.object(workspace_manager, "_ownership_operation_lock", contended_operation_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.drive, owners[i], runs[i], knowledge[i], f"worker-{i}")
                           for i in range(2)]
                parked = [future.result(timeout=20) for future in futures]
        self.assertEqual(observed, {"active": 0, "maximum_active": 1, "entrants": 2})
        for result in parked:
            self.assertEqual((result["reason_code"], result["parked"], result["gate"]),
                             ("PARKED", "APPROVAL_WAIT", "G4"), result)
        rows = [self.ownership(owners[i], runs[i]) for i in range(2)]
        self.assertTrue(set(row["worktree_path"] for row in rows[0].values()).isdisjoint(
            row["worktree_path"] for row in rows[1].values()))
        self.assertTrue(set(row["owner_token"] for row in rows[0].values()).isdisjoint(
            row["owner_token"] for row in rows[1].values()))
        for index in range(2):
            for row in rows[index].values():
                self.assertEqual(row["run_id"], runs[index])
                self.assertEqual(self.git(row["worktree_path"], "rev-parse", "HEAD").strip(),
                                 row["baseline_revision"])
                self.assertIn(row["worktree_path"], self.git(row["repo_path"], "worktree", "list", "--porcelain"))
                self.assertEqual(owners[index].workspaces.query_ownership(
                    row["repo_path"], runs[index], row["task_id"], row["owner_token"])["status"], "VERIFIED")
            self.fixture._approval(runs[index], "G4", parked[index]["approval_input_hash"])

        # The approved re-entry must retain each run's own worktrees and gate identity.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.drive, owners[i], runs[i], knowledge[i], f"resume-{i}")
                       for i in range(2)]
            resumed = [future.result(timeout=20) for future in futures]
        for index in range(2):
            self.assertEqual(resumed[index]["parked"], "PRODUCER_WAIT", resumed[index])
            self.assertEqual(owners[index].status(runs[index])["state"], "PLAN")
            self.assertEqual(self.ownership(owners[index], runs[index]), rows[index])
            events = owners[index].state.events(runs[index])
            replay = self.drive(owners[index], runs[index], knowledge[index], f"replay-{index}")
            self.assertEqual(replay["producer_job"]["job_id"], resumed[index]["producer_job"]["job_id"])
            self.assertEqual(owners[index].state.events(runs[index]), events)
            self.assertEqual(self.lease_history(f"worker-run:{runs[index]}"),
                             ["ACQUIRE", "RELEASE"] * 3)
        for repo in repos:
            source = before[str(repo)]
            self.assertEqual(self.git(repo, "rev-parse", "HEAD"), source["head"])
            self.assertEqual(self.git(repo, "branch", "--show-current"), source["branch"])
            self.assertEqual(self.git(repo, "status", "--porcelain"), source["status"])
            self.assertEqual((repo / "README.md").read_bytes(), source["tracked"])
            self.assertEqual((repo / "user-untracked.txt").read_bytes(), source["untracked"])
        self.assertEqual(self.owner.recovery.locks.records()["active"], [])

    def test_same_run_worker_lease_blocks_a_second_real_worker_before_workspace_effects(self):
        run_id, knowledge = self.run_at_workspace("BGW-9511")
        other = Orchestrator(self.fixture.root)
        entered = threading.Event()
        release = threading.Event()
        original = self.owner.workspaces.create

        def pause_first_create(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release the first worker")
            return original(*args, **kwargs)

        with patch.object(self.owner.workspaces, "create", side_effect=pause_first_create):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.drive, self.owner, run_id, knowledge, "first")
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    before = self.owner.state.events(run_id)
                    blocked = self.drive(other, run_id, knowledge, "second")
                    self.assertEqual(blocked["reason_code"], "WORKER_LEASE_HELD", blocked)
                    self.assertEqual(self.owner.state.events(run_id), before)
                    self.assertEqual(_ownership_rows(other, run_id), {})
                finally:
                    release.set()
                parked = first.result(timeout=20)
        self.assertEqual(parked["parked"], "APPROVAL_WAIT", parked)
        self.ownership(self.owner, run_id)
        self.assertEqual(self.lease_history(f"worker-run:{run_id}"), ["ACQUIRE", "RELEASE"])
        self.assertEqual(self.owner.recovery.locks.records()["active"], [])

    def test_expired_dead_worker_lease_is_taken_over_and_replay_keeps_owned_worktrees(self):
        run_id, knowledge = self.run_at_workspace("BGW-9521")
        key = f"worker-run:{run_id}"
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        crashed = LockManager(self.owner.state.database_path, pid=2147483000, clock=lambda: expired)
        self.assertTrue(crashed.acquire(key, "crashed", 1)["acquired"])
        parked = self.drive(self.owner, run_id, knowledge, "replacement")
        self.assertEqual(parked["parked"], "APPROVAL_WAIT", parked)
        rows = self.ownership(self.owner, run_id)
        replay = self.drive(self.owner, run_id, knowledge, "replay")
        self.assertEqual(replay["approval_input_hash"], parked["approval_input_hash"])
        self.assertEqual(self.ownership(self.owner, run_id), rows)
        self.assertEqual(self.lease_history(key), ["ACQUIRE", "TAKEOVER", "RELEASE", "ACQUIRE", "RELEASE"])
        self.assertEqual(self.owner.recovery.locks.records()["active"], [])

    def test_locks_none_is_single_process_mode_and_does_not_consult_the_run_lease(self):
        run_id, knowledge = self.run_at_workspace("BGW-9531")
        key = f"worker-run:{run_id}"
        locks = self.owner.recovery.locks
        self.assertTrue(locks.acquire(key, "host-held", 300)["acquired"])
        self.addCleanup(locks.release, key, "host-held")
        parked = self.drive(self.owner, run_id, knowledge, "unlocked", locked=False)
        self.assertEqual(parked["parked"], "APPROVAL_WAIT", parked)
        rows = self.ownership(self.owner, run_id)
        replay = self.drive(self.owner, run_id, knowledge, "unlocked-replay", locked=False)
        self.assertEqual(replay["approval_input_hash"], parked["approval_input_hash"])
        self.assertEqual(self.ownership(self.owner, run_id), rows)
        self.assertEqual(self.lease_history(key), ["ACQUIRE"])
        self.assertEqual(locks.records()["active"][0]["owner_token"], "host-held")
        blocked = self.drive(self.owner, run_id, knowledge, "locked")
        self.assertEqual(blocked["reason_code"], "WORKER_LEASE_HELD", blocked)


if __name__ == "__main__":
    unittest.main()
