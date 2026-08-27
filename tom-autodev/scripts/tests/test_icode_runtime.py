import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
from clients.icode_runtime import IcodeRuntime
from state_store import StateStore
from workspace_manager import WorkspaceManager


class FakeArgvTransport:
    def __init__(self, *, help_text="Commands:\n  api\n  git\n  login\n", login_code=0):
        self.help_text = help_text
        self.login_code = login_code
        self.calls = []
        self.changes = []
        self.push_result = {"returncode": 0, "stdout": "submitted"}
        self.on_push = None

    def run(self, argv, *, cwd=None, timeout=None):
        self.calls.append((list(argv), str(cwd) if cwd else None, timeout))
        if argv[-1] == "--help":
            return {"returncode": 0, "stdout": self.help_text, "stderr": ""}
        if argv[-1] == "login":
            return {"returncode": self.login_code, "stdout": "", "stderr": "login failed"}
        if "get_repo_reviews" in argv:
            return {
                "returncode": 0,
                "stdout": json.dumps({"data": {"changes": self.changes}}),
                "stderr": "",
            }
        if "push_cr" in argv:
            if self.on_push is not None:
                self.on_push()
            if isinstance(self.push_result, Exception):
                raise self.push_result
            return dict(self.push_result)
        raise AssertionError(f"unexpected argv: {argv}")


def approved(ledger, run_id, action, input_hash):
    policy = {"comate": ["owner@example.test"], "infoflow": ["owner@example.test"]}
    row = ledger.request(action, input_hash, ["comate", "infoflow"], run_id=run_id, member_policy=policy)
    for channel in ("comate", "infoflow"):
        ledger.record_delivery(row["approval_id"], channel, {"request_id": f"{channel}-1"}, payload_hash=input_hash)
    ledger.resolve(row["approval_id"], "APPROVE", input_hash, "comate", run_id=run_id, responder="owner@example.test")
    return {"approval_id": row["approval_id"], "input_hash": input_hash}


class IcodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_repo = self.root / "repo"
        self.source_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source_repo)], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "config", "user.email", "dev@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "config", "user.name", "dev"], check=True)
        (self.source_repo / "file.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source_repo), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "commit", "-qm", "CARD-1 initial"], check=True)
        self.revision = subprocess.check_output(["git", "-C", str(self.source_repo), "rev-parse", "HEAD"], text=True).strip()
        self.workspaces = WorkspaceManager(self.root / "worktrees")
        self.workspace = self.workspaces.create(
            self.source_repo, "run-1", "task-1",
            {"baseline_status": "READY", "baseline_revision": self.revision},
        )
        self.repo = Path(self.workspace["worktree_path"])
        self.skill = self.root / "system-icode"
        self.skill.mkdir()
        self.state = StateStore(self.root / "state.sqlite")
        self.ledger = ApprovalLedger(self.root / "approvals.sqlite")
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.transport = FakeArgvTransport()

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, **changes):
        binding = {
            "run_id": "run-1",
            "baseline_revision": self.revision,
            "module": "baidu/team/repo",
            "target_branch": "main",
            "repo_path": str(self.source_repo.resolve()),
            "task_id": "task-1",
            "owner_token": self.workspace["owner_token"],
            "worktree_path": str(self.repo.resolve()),
        }
        values = {
            "state_store": self.state,
            "approval_ledger": self.ledger,
            "artifact_store": self.artifacts,
            "workspace_manager": self.workspaces,
            "run_id": "run-1",
            "worktree_bindings": {str(self.repo.resolve()): binding},
            "system_skill_path": self.skill,
            "argv_transport": self.transport,
            "binary_candidates": ["/fake/icode"],
            "executable_resolver": lambda value: value,
            "owner": "dev",
        }
        values.update(changes)
        return IcodeRuntime(**values)

    def set_ownership_status(self, status):
        database = self.root / ".worktrees.tom-autodev-ownership.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE worktree_ownership SET status = ? WHERE worktree_path = ?",
                (status, str(self.repo.resolve())),
            )

    def preflight_snapshot(self):
        database = self.root / ".worktrees.tom-autodev-ownership.sqlite"

        def git(path, *args):
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return (result.returncode, result.stdout, result.stderr)

        return {
            "ownership_bytes": database.read_bytes(),
            "source_exists": self.source_repo.exists(),
            "worktree_exists": self.repo.exists(),
            "registration": git(self.source_repo, "worktree", "list", "--porcelain"),
            "source_status": git(self.source_repo, "status", "--porcelain=v1"),
            "worktree_status": git(self.repo, "status", "--porcelain=v1"),
        }

    def change_set(self):
        revisions = {
            "business": {"module": "baidu/team/repo", "revision": self.revision, "branch": "main"},
            "test": {"module": "baidu/team/repo-tests", "revision": "test-rev", "branch": "main"},
        }
        return self.persist_reviewed_change_set({
            "run_id": "run-1",
            "change_set_id": "change-1",
            "revision_set_id": "revisions-1",
            "input_hash": "pending",
            "repo_path": str(self.repo),
            "module": "baidu/team/repo",
            "target_branch": "main",
            "commit_revision": self.revision,
            "card_id": "CARD-1",
            "owner": "dev",
            "revision_set": revisions,
        })

    def persist_reviewed_change_set(self, change):
        reviewed = {
            key: change[key]
            for key in (
                "run_id", "change_set_id", "revision_set_id", "repo_path", "module",
                "target_branch", "commit_revision", "card_id", "owner", "revision_set",
            )
        }
        content = json.dumps(reviewed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        artifact = self.artifacts.put(
            "run-1", "change-set", content,
            {"verdict": "PASS", "revision_set_id": change["revision_set_id"]},
        )
        change["reviewed_artifact_id"] = artifact["artifact_id"]
        change["input_hash"] = hashlib.sha256(content).hexdigest()
        return change

    def test_preflight_rejects_missing_system_skill_binary_subcommand_and_login(self):
        self.skill.rmdir()
        self.assertEqual(self.runtime().preflight(self.repo)["reason_code"], "ICODE_SKILL_NOT_FOUND")
        self.skill.mkdir()
        self.assertEqual(self.runtime(executable_resolver=lambda _value: None).preflight(self.repo)["reason_code"], "ICODE_CLI_NOT_FOUND")
        self.transport.help_text = "Commands:\n api\n git\n"
        self.assertEqual(self.runtime().preflight(self.repo)["reason_code"], "ICODE_SUBCOMMAND_MISSING")
        self.transport.help_text = "Commands:\n api\n git\n login\n"
        self.transport.login_code = 1
        self.assertEqual(self.runtime().preflight(self.repo)["reason_code"], "ICODE_LOGIN_REQUIRED")

    def test_preflight_is_read_only_and_rejects_stale_or_foreign_worktree(self):
        before = subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain=v1"], text=True)
        durable = self.runtime().worktree_bindings[str(self.repo.resolve())]
        foreign_binding = dict(durable, run_id="other")
        stale_binding = dict(durable, baseline_revision="0" * 40)
        foreign = self.runtime(worktree_bindings={str(self.repo.resolve()): foreign_binding}).preflight(self.repo)
        stale = self.runtime(worktree_bindings={str(self.repo.resolve()): stale_binding}).preflight(self.repo)
        after = subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain=v1"], text=True)
        self.assertEqual(foreign["reason_code"], "WORKTREE_RUN_MISMATCH")
        self.assertEqual(stale["reason_code"], "WORKTREE_NOT_OWNED")
        self.assertEqual(after, before)

    def test_preflight_active_ownership_is_query_only_and_never_calls_reconcile(self):
        before = self.preflight_snapshot()
        with patch.object(
            self.workspaces,
            "reconcile",
            side_effect=AssertionError("preflight must not reconcile ownership"),
        ):
            result = self.runtime().preflight(self.repo)
        after = self.preflight_snapshot()

        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(after, before)

    def test_preflight_reserved_ownership_fails_closed_without_mutation(self):
        self.set_ownership_status("RESERVED")
        before = self.preflight_snapshot()
        result = self.runtime().preflight(self.repo)
        after = self.preflight_snapshot()

        self.assertEqual(after, before)
        self.assertEqual(result["reason_code"], "WORKTREE_NOT_ACTIVE")

    def test_preflight_failed_ownership_fails_closed_without_mutation(self):
        self.set_ownership_status("FAILED")
        before = self.preflight_snapshot()
        result = self.runtime().preflight(self.repo)
        after = self.preflight_snapshot()

        self.assertEqual(after, before)
        self.assertEqual(result["reason_code"], "WORKTREE_NOT_ACTIVE")

    def test_preflight_cleaning_ownership_fails_closed_without_mutation(self):
        self.set_ownership_status("CLEANING")
        before = self.preflight_snapshot()
        result = self.runtime().preflight(self.repo)
        after = self.preflight_snapshot()

        self.assertEqual(after, before)
        self.assertEqual(result["reason_code"], "WORKTREE_NOT_ACTIVE")

    def test_preflight_rejects_primary_checkout_even_with_caller_asserted_run_binding(self):
        forged = dict(self.runtime().worktree_bindings[str(self.repo.resolve())])
        forged["worktree_path"] = str(self.source_repo.resolve())
        result = self.runtime(worktree_bindings={str(self.source_repo.resolve()): forged}).preflight(self.source_repo)
        self.assertEqual(result["reason_code"], "WORKTREE_NOT_OWNED")

    def test_submit_requires_reviewed_exact_revision_set_and_run_bound_g7(self):
        change = self.change_set()
        change.pop("reviewed_artifact_id")
        no_review = self.runtime().submit(change, {"approval_id": "missing", "input_hash": change["input_hash"]})
        change = self.change_set()
        change["revision_set"]["test"]["revision"] = "different"
        mismatch = self.runtime().submit(change, {"approval_id": "missing", "input_hash": change["input_hash"]})
        approved_change = self.change_set()
        wrong_gate = approved(self.ledger, "run-1", "G8", approved_change["input_hash"])
        rejected = self.runtime().submit(approved_change, wrong_gate)
        self.assertEqual(no_review["reason_code"], "CHANGE_SET_REVIEW_REQUIRED")
        self.assertEqual(mismatch["reason_code"], "REVIEW_ARTIFACT_MISMATCH")
        self.assertEqual(rejected["reason_code"], "APPROVAL_GATE_MISMATCH")
        self.assertFalse(any("push_cr" in call[0] for call in self.transport.calls))

    def test_submit_recomputes_hash_from_persisted_reviewed_change_set(self):
        original = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", original["input_hash"])
        changed = json.loads(json.dumps(original))
        changed["revision_set"]["test"]["revision"] = "changed-test-revision"
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 exact", "url": "https://icode.example/cr/42",
        }]

        result = self.runtime().submit(changed, approval)

        self.assertEqual(result["reason_code"], "REVIEW_ARTIFACT_MISMATCH")
        self.assertFalse(any("push_cr" in call[0] for call in self.transport.calls))

    def test_exact_existing_cr_is_receipted_without_push_and_raw_push_is_never_used(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 exact", "url": "https://icode.example/cr/42",
        }]
        result = self.runtime().submit(change, approval)
        argv = [part for call in self.transport.calls for part in call[0]]
        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["change_number"], "42")
        self.assertNotIn("push_cr", argv)
        self.assertNotIn("push", argv)

    def test_receipt_contains_complete_reviewed_revisions_and_strong_cr_identity(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 exact", "url": "https://icode.example/cr/42",
            "module": "baidu/team/repo",
        }]
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["revision_set"]["test"]["revision"], "test-rev")
        self.assertIn("revision-test-rev", result["evidence_refs"])

    def test_receipt_rejects_cr_url_for_a_different_change_number(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 exact", "url": "https://icode.example/cr/999",
            "module": "baidu/team/repo",
        }]
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["reason_code"], "CR_RECEIPT_INVALID")

    def test_related_existing_cr_with_conflicting_identity_fails_closed(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": "f" * 40, "branch": "release",
            "owner": {"username": "someone-else"}, "subject": "CARD-1 stale", "url": "https://icode.example/cr/42",
        }]
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["reason_code"], "CR_IDENTITY_CONFLICT")
        self.assertFalse(any("push_cr" in call[0] for call in self.transport.calls))

    def test_secret_bearing_cr_url_is_not_receipted(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 exact",
            "url": "https://icode.example/cr/42?token=must-not-persist",
        }]
        result = self.runtime().submit(change, approval)
        database_bytes = (self.root / "state.sqlite").read_bytes()
        self.assertEqual(result["reason_code"], "CR_RECEIPT_INVALID")
        self.assertNotIn(b"must-not-persist", database_bytes)

    def test_unknown_submit_is_pending_and_restart_only_queries(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.push_result = TimeoutError("unknown")
        first = self.runtime().submit(change, approval)
        second = self.runtime().submit(change, approval)
        pushes = [call for call in self.transport.calls if "push_cr" in call[0]]
        self.assertEqual(first["reason_code"], "SUBMIT_RESULT_UNKNOWN")
        self.assertEqual(second["reason_code"], "QUERY_REQUIRED")
        self.assertFalse(second["retry_allowed"])
        self.assertEqual(len(pushes), 1)

    def test_concurrent_submit_has_one_writer_and_completed_receipt_replays(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        entered = threading.Event()
        release = threading.Event()

        def finish_push():
            entered.set()
            release.wait(2)
            self.transport.changes = [{
                "_number": 43, "current_revision": self.revision, "branch": "main",
                "owner": {"username": "dev"}, "subject": "CARD-1 exact", "url": "https://icode.example/cr/43",
            }]

        self.transport.on_push = finish_push
        results = []
        thread = threading.Thread(target=lambda: results.append(self.runtime().submit(change, approval)))
        thread.start()
        self.assertTrue(entered.wait(2))
        non_owner = self.runtime().submit(change, approval)
        release.set()
        thread.join(2)
        replay = self.runtime().submit(change, approval)
        self.assertEqual(non_owner["reason_code"], "QUERY_REQUIRED")
        self.assertEqual(results[0]["reason_code"], "OK")
        self.assertEqual(replay["change_number"], "43")
        self.assertEqual(sum("push_cr" in call[0] for call in self.transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
