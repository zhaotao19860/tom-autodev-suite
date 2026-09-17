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
from clients.icode_runtime import IcodeRuntime, _icode_remote_url
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
        if "push_cr" in argv or any(str(part).startswith("refs/for/") or ":refs/for/" in str(part) for part in argv):
            if self.on_push is not None:
                self.on_push()
            if isinstance(self.push_result, Exception):
                raise self.push_result
            return dict(self.push_result)
        raise AssertionError(f"unexpected argv: {argv}")


def _is_push(argv):
    """Creating a CR must go through iCode's push_cr boundary."""
    return "push_cr" in argv


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
        subprocess.run(
            ["git", "-C", str(self.source_repo), "commit", "-qm", "CARD-1 initial\n\nChange-Id: Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
            check=True,
        )
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

    def change_set(self, commit_revision=None):
        commit_revision = commit_revision or self.revision
        revisions = {
            "business": {"module": "baidu/team/repo", "revision": commit_revision, "branch": "main"},
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
            "commit_revision": commit_revision,
            "card_id": "CARD-1",
            "owner": "dev",
            "revision_set": revisions,
        })

    def child_change_set(self):
        (self.repo / "file.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "file.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "CARD-1 child\n\nChange-Id: Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
            check=True,
        )
        commit_revision = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        return self.change_set(commit_revision)

    def persist_reviewed_change_set(self, change):
        reviewed = {
            key: change[key]
            for key in (
                "run_id", "change_set_id", "revision_set_id", "repo_path", "module",
                "target_branch", "commit_revision", "card_id", "owner", "revision_set",
            )
        }
        if "submission_mode" in change:
            reviewed["submission_mode"] = change["submission_mode"]
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

    def test_preflight_accepts_a_worktree_carrying_the_change_set_commit(self):
        """A change set sits on top of its baseline, so HEAD moving ahead is normal.

        Both the ownership query and preflight used to require HEAD to equal the
        recorded baseline, which modelled a checkout nobody had worked in and so
        rejected every real submission.
        """
        baseline = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.repo / "change.txt").write_text("submitted work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t",
             "-c", "user.email=t@example.test", "commit", "-q", "-m", "change set"],
            check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

        result = self.runtime().preflight(self.repo)

        self.assertNotEqual(head, baseline)
        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["revision"], head)

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
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

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
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

    def test_submit_materializes_worktree_for_icode_cli_and_cleans_it_after_push(self):
        subprocess.run(
            [
                "git", "-C", str(self.source_repo), "remote", "add", "origin",
                "ssh://dev@icode.baidu.com:8235/baidu/team/repo",
            ],
            check=True,
        )
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        seen = {}

        def observe_push():
            push = next(call for call in self.transport.calls if _is_push(call[0]))
            cli_repo = Path(push[1])
            seen["path"] = cli_repo
            seen["git_is_directory"] = (cli_repo / ".git").is_dir()
            seen["revision"] = subprocess.check_output(
                ["git", "-C", str(cli_repo), "rev-parse", "HEAD"], text=True
            ).strip()
            seen["origin"] = subprocess.check_output(
                ["git", "-C", str(cli_repo), "remote", "get-url", "origin"], text=True
            ).strip()
            self.transport.changes = [{
                "_number": 42,
                "current_revision": change["commit_revision"],
                "branch": "main",
                "owner": {"username": "dev"},
                "subject": "CARD-1 exact",
                "url": "https://icode.example/cr/42",
            }]

        self.transport.on_push = observe_push
        result = self.runtime().submit(change, approval)

        self.assertEqual(result["reason_code"], "OK")
        self.assertTrue(seen["git_is_directory"])
        self.assertEqual(seen["revision"], change["commit_revision"])
        self.assertTrue(_icode_remote_url(seen["origin"]))
        self.assertFalse(seen["path"].exists())

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
        # Related now means "the same Gerrit identity": the revision this submission
        # names, or the Change-Id its commit carries. A CR at our revision but owned by
        # someone else on another branch is the conflict worth failing closed on.
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "release",
            "owner": {"username": "someone-else"}, "subject": "CARD-1 stale", "url": "https://icode.example/cr/42",
        }]
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["reason_code"], "CR_IDENTITY_CONFLICT")
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

    def test_a_sibling_cr_for_the_same_card_does_not_block_this_submission(self):
        # A requirement that spans tasks has several CRs in one repository. An open CR
        # for the same card at a different revision is a sibling, not this submission,
        # so it must not be mistaken for an identity conflict.
        change = self.child_change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 41, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 earlier task",
            "url": "https://icode.example/cr/41",
        }]
        self.transport.on_push = lambda: self.transport.changes.__setitem__(0, {
            **self.transport.changes[0],
            "current_revision": change["commit_revision"],
        })

        result = self.runtime().submit(change, approval)

        self.assertNotEqual(result["reason_code"], "CR_IDENTITY_CONFLICT")
        self.assertTrue(any(_is_push(call[0]) for call in self.transport.calls))

    def test_an_abandoned_attempt_can_be_superseded_once_the_cr_is_known_absent(self):
        # Abandoning an intent records that we stopped waiting, not that the write
        # landed. With reconcile reporting the CR absent, a later attempt must be able
        # to push; otherwise a run abandoned over a since-fixed bug stays wedged.
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        # First attempt: a CR at our revision owned by someone else stops the submission
        # before any push, which is how the intent came to exist.
        self.transport.changes = [{
            "_number": 42, "current_revision": self.revision, "branch": "release",
            "owner": {"username": "someone-else"}, "subject": "CARD-1 stale",
            "url": "https://icode.example/cr/42",
        }]
        blocked = self.runtime().submit(change, approval)
        self.assertEqual(blocked["reason_code"], "CR_IDENTITY_CONFLICT")
        key = f"icode.submit:run-1:{change['change_set_id']}:{change['revision_set_id']}"
        self.state.abandon_intent(
            self.state.intent_by_idempotency_key(key)["intent_id"],
            "blocked before push, nothing written", "tester",
        )
        self.transport.changes = []

        result = self.runtime().submit(change, approval)

        self.assertNotEqual(result.get("reason_code"), "INTENT_ABANDONED")
        self.assertNotEqual(result.get("reason_code"), "QUERY_REQUIRED")
        self.assertTrue(any(_is_push(call[0]) for call in self.transport.calls))

    def test_a_base_the_branch_has_left_behind_is_refused_before_any_push(self):
        # A worktree is cut once and the branch keeps moving. Pushing from a base the
        # branch has passed produces a CR Gerrit marks as conflicting, which reads like a
        # defect in the change; the branch state is checkable before writing anything.
        upstream = self.root / "upstream"
        subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "remote", "add", "origin", str(upstream)], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
        (self.source_repo / "moved.txt").write_text("theirs\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source_repo), "add", "moved.txt"], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "commit", "-qm", "CARD-1 someone else"], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
        subprocess.run(["git", "-C", str(self.source_repo), "reset", "-q", "--hard", "HEAD~1"], check=True)
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])

        result = self.runtime().submit(change, approval)

        self.assertEqual(result["reason_code"], "BASELINE_BEHIND_REMOTE")
        self.assertEqual(result["commits_behind"], "1")
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

    def test_one_cr_per_repo_appends_the_sibling_cr_instead_of_refusing(self):
        # Reconcile has already said no CR carries this change set, so an open CR for the
        # same card in the same repository is a sibling. Under this policy the content
        # belongs there as a further patchset, and the boundary says which CR that is
        # rather than squashing two reviewed change sets on its own.
        change = self.child_change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 41, "current_revision": self.revision, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 earlier task",
            "url": "https://icode.example/cr/41",
        }]
        self.transport.on_push = lambda: self.transport.changes.__setitem__(0, {
            **self.transport.changes[0],
            "current_revision": change["commit_revision"],
        })

        strict = self.runtime(submission_policy="one_cr_per_repo").submit(change, approval)

        self.assertNotEqual(strict["reason_code"], "REPO_CR_ALREADY_OPEN")
        self.assertTrue(any(_is_push(call[0]) for call in self.transport.calls))

    def test_one_cr_per_repo_refuses_non_ancestor_existing_cr_revision_before_push(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 41,
            "current_revision": "f" * 40,
            "branch": "main",
            "owner": {"username": "dev"},
            "subject": "CARD-1 earlier task",
            "url": "https://icode.example/cr/41",
        }]

        result = self.runtime(submission_policy="one_cr_per_repo").submit(change, approval)

        self.assertEqual(result["reason_code"], "CR_BASELINE_DRIFT")
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

    def test_the_default_policy_still_opens_a_cr_per_change_set(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 41, "current_revision": "f" * 40, "branch": "main",
            "owner": {"username": "dev"}, "subject": "CARD-1 earlier task",
            "url": "https://icode.example/cr/41",
        }]

        result = self.runtime().submit(change, approval)

        self.assertNotEqual(result["reason_code"], "REPO_CR_ALREADY_OPEN")
        self.assertTrue(any(_is_push(call[0]) for call in self.transport.calls))

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

    def test_missing_change_id_is_refused_before_push(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "CARD-1 no trailer"],
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        change = self.change_set(revision)
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["reason_code"], "CHANGE_ID_MISSING")
        self.assertFalse(any(_is_push(call[0]) for call in self.transport.calls))

    def test_submit_rejected_receipts_bounded_stderr(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.push_result = {
            "returncode": 1,
            "stdout": "hint",
            "stderr": "missing Change-Id\ntoken: super-secret\n",
        }
        result = self.runtime().submit(change, approval)
        self.assertEqual(result["reason_code"], "SUBMIT_REJECTED")
        self.assertIn("missing Change-Id", result["cli_stderr"])
        self.assertNotIn("super-secret", result["cli_stderr"])
        stored = self.state.result_by_idempotency_key(
            f"icode.submit:run-1:{change['change_set_id']}:{change['revision_set_id']}"
        )
        self.assertEqual(stored["receipt"]["response"]["reason_code"], "SUBMIT_REJECTED")
        self.assertIn("missing Change-Id", stored["receipt"]["response"]["cli_stderr"])
        self.assertNotIn("super-secret", stored["receipt"]["response"]["cli_stderr"])

    def test_submit_rejected_without_cr_can_be_retried_after_skill_fix(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.push_result = {
            "returncode": 1,
            "stdout": "",
            "stderr": "failed to extract repo name from remote URL: /tmp/worktree\n",
        }
        first = self.runtime().submit(change, approval)
        self.transport.push_result = {"returncode": 0, "stdout": "submitted", "stderr": ""}
        def finish_push():
            self.transport.changes = [{
                "_number": 55, "current_revision": self.revision, "branch": "main",
                "owner": {"username": "dev"}, "subject": "CARD-1 exact",
                "url": "https://icode.example/cr/55",
            }]
        self.transport.on_push = finish_push
        second = self.runtime().submit(change, approval)
        self.assertEqual(first["reason_code"], "SUBMIT_REJECTED")
        self.assertEqual(second["reason_code"], "OK")
        self.assertEqual(second["change_number"], "55")
        self.assertEqual(sum(_is_push(call[0]) for call in self.transport.calls), 2)

    def test_unknown_submit_is_pending_and_restart_only_queries(self):
        change = self.change_set()
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.push_result = TimeoutError("unknown")
        first = self.runtime().submit(change, approval)
        second = self.runtime().submit(change, approval)
        pushes = [call for call in self.transport.calls if _is_push(call[0])]
        self.assertEqual(first["reason_code"], "SUBMIT_RESULT_UNKNOWN")
        self.assertEqual(second["reason_code"], "QUERY_REQUIRED")
        self.assertFalse(second["retry_allowed"])
        self.assertEqual(len(pushes), 1)

    def test_one_cr_per_repo_create_new_cr_skips_baseline_drift(self):
        change = self.change_set()
        change["submission_mode"] = "create_new_cr"
        change = self.persist_reviewed_change_set(change)
        approval = approved(self.ledger, "run-1", "G7", change["input_hash"])
        self.transport.changes = [{
            "_number": 41,
            "current_revision": "f" * 40,
            "branch": "main",
            "owner": {"username": "dev"},
            "subject": "CARD-1 earlier task",
            "url": "https://icode.example/cr/41",
        }]
        self.transport.on_push = lambda: self.transport.changes.__setitem__(0, {
            "_number": 99,
            "current_revision": self.revision,
            "branch": "main",
            "owner": {"username": "dev"},
            "subject": "CARD-1 exact",
            "url": "https://icode.example/cr/99",
        })
        result = self.runtime(submission_policy="one_cr_per_repo").submit(change, approval)
        self.assertEqual(result["reason_code"], "OK")
        self.assertEqual(result["change_number"], "99")
        self.assertTrue(any(_is_push(call[0]) for call in self.transport.calls))


if __name__ == "__main__":
    unittest.main()
