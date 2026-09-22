from __future__ import annotations

from execution_guard import guard_execution

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import yaml

from approval_ledger import ApprovalLedger, gate_of
from artifact_store import ArtifactStore
from profile_repin import pinned_hash, record_for
from state_store import StateStore
from workspace_manager import WorkspaceManager


_REQUIRED_COMMANDS = ("api", "git", "login")
_HEX_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")


class IcodeRuntime:
    """Run-bound, restart-safe adapter for the system iCode command."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        approval_ledger: ApprovalLedger,
        artifact_store: ArtifactStore,
        workspace_manager: WorkspaceManager,
        run_id: str,
        worktree_bindings: dict[str, dict[str, Any]],
        system_skill_path: Path | str,
        argv_transport: Any,
        binary_candidates: Iterable[str] | None = None,
        executable_resolver: Callable[[str], str | None] | None = None,
        owner: str = "",
        submission_policy: str | None = None,
        profile_hash: str | None = None,
    ):
        self.state = state_store
        self.approvals = approval_ledger
        self.artifacts = artifact_store
        self.workspaces = workspace_manager
        self.run_id = run_id
        self.worktree_bindings = worktree_bindings
        self.system_skill_path = Path(system_skill_path).expanduser()
        self.transport = argv_transport
        self.binary_candidates = list(binary_candidates or _default_candidates())
        self.executable_resolver = executable_resolver or _resolve_executable
        self.owner = owner
        # `one_cr_per_repo` means a requirement keeps a single open CR per repository, so
        # a second change set for the same card folds into it instead of opening another.
        self.submission_policy = submission_policy or "one_cr_per_change_set"
        self._bound_submission_policy = self.submission_policy
        self._bound_profile_hash = profile_hash or pinned_hash(
            self.state.events(run_id), record_for(self.state, run_id))
        self._cli_by_repo: dict[str, str] = {}

    def _profile_error(self) -> dict[str, Any] | None:
        """Refuse fresh effects when the disk profile or this client's pin changed."""
        events = self.state.events(self.run_id)
        intake = events[0].get("payload") if events else None
        # Legacy standalone adapters have no disk profile contract. The controller
        # factory always requires one, including when reconstructing a cached client.
        if not isinstance(intake, dict) or "profile_path" not in intake:
            return None
        path = intake.get("profile_path")
        expected = pinned_hash(events, record_for(self.state, self.run_id))
        if not _nonempty(path) or not _nonempty(expected):
            return _failure("PROJECT_NOT_READY")
        if (expected != self._bound_profile_hash
                or self.submission_policy != self._bound_submission_policy):
            return _failure("PROFILE_CONFLICT")
        try:
            raw = Path(path).read_bytes()
        except OSError:
            return _failure("PROJECT_NOT_READY")
        if hashlib.sha256(raw).hexdigest() != expected:
            return _failure("PROFILE_CONFLICT")
        try:
            profile = yaml.safe_load(raw)
        except (UnicodeDecodeError, yaml.YAMLError):
            return _failure("PROJECT_NOT_READY")
        if not isinstance(profile, dict):
            return _failure("PROJECT_NOT_READY")
        policy = profile.get("submission_policy") or "one_cr_per_change_set"
        if policy != self.submission_policy:
            return _failure("PROFILE_CONFLICT")
        return None

    def preflight(self, repo_path: Path) -> dict[str, Any]:
        path = Path(repo_path).expanduser().resolve()
        binding = self.worktree_bindings.get(str(path))
        if not isinstance(binding, dict):
            return _failure("WORKTREE_NOT_REGISTERED")
        if binding.get("run_id") != self.run_id:
            return _failure("WORKTREE_RUN_MISMATCH")
        if not _nonempty(binding.get("module")) or not _nonempty(binding.get("target_branch")):
            return _failure("WORKTREE_BINDING_INVALID")
        ownership_fields = ("repo_path", "task_id", "owner_token", "worktree_path")
        if any(not _nonempty(binding.get(field)) for field in ownership_fields):
            return _failure("WORKTREE_NOT_OWNED")
        try:
            ownership = self.workspaces.query_ownership(
                binding["repo_path"], self.run_id, binding["task_id"], binding["owner_token"]
            )
        except Exception:
            return _failure("WORKTREE_OWNERSHIP_QUERY_FAILED")
        if (
            ownership.get("status") != "VERIFIED"
            or ownership.get("ownership_status") != "ACTIVE"
            or Path(str(ownership.get("worktree_path"))).resolve()
            != Path(str(binding.get("ownership_worktree_path", path))).resolve()
            or ownership.get("baseline_revision") != binding.get("baseline_revision")
        ):
            return _failure(ownership.get("reason_code") or "WORKTREE_NOT_OWNED")
        try:
            inside = _git(path, "rev-parse", "--is-inside-work-tree")
            top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
            revision = _git(path, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return _failure("GIT_WORKTREE_INVALID")
        if inside != "true" or top != path:
            return _failure("GIT_WORKTREE_INVALID")
        # HEAD is reported, not required to equal the baseline. The baseline identifies
        # the workspace we own; HEAD is the commit being submitted, and a change set
        # always sits on top of its baseline, so requiring them to match would reject
        # every real submission. `submit` compares HEAD against the change set's own
        # `commit_revision`, which is the question that actually matters here.
        if not self.system_skill_path.is_dir():
            return _failure("ICODE_SKILL_NOT_FOUND")
        cli_result = self._discover_cli(path)
        if cli_result.get("reason_code") != "OK":
            return cli_result
        cli = cli_result["cli"]
        login = _run(self.transport, [cli, "login"], cwd=path, timeout=30)
        if login["returncode"] != 0:
            return _failure("ICODE_LOGIN_REQUIRED")
        self._cli_by_repo[str(path)] = cli
        return {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "repo_path": str(path),
            "revision": revision,
            "module": binding["module"],
            "target_branch": binding["target_branch"],
            "cli": cli,
        }

    @guard_execution
    def submit(self, change_set: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        invalid = self._validate_change_set(change_set)
        if invalid is not None:
            return invalid
        approval_result = _approved_record(
            self.approvals, approval, run_id=self.run_id, action="G7", input_hash=change_set["input_hash"]
        )
        if approval_result is not None:
            return approval_result
        repo_path = Path(change_set["repo_path"]).expanduser().resolve()
        preflight = self.preflight(repo_path)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if preflight.get("reason_code") != "OK":
            return preflight
        if preflight["module"] != change_set["module"] or preflight["target_branch"] != change_set["target_branch"]:
            return _failure("CHANGE_SET_WORKTREE_MISMATCH")
        if preflight["revision"] != change_set["commit_revision"]:
            return _failure("STALE_BASELINE")
        unpushable = _unpushable_commit(repo_path, change_set)
        if unpushable is not None:
            return unpushable
        drift = self._remote_drift(repo_path, change_set)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if drift is not None:
            return drift

        payload = {
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "input_hash": change_set["input_hash"],
            "approval_id": approval["approval_id"],
            "repo_path": str(repo_path),
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "card_id": change_set["card_id"],
            "owner": change_set["owner"],
            "revision_set": change_set["revision_set"],
        }
        base_key = f"icode.submit:{self.run_id}:{change_set['change_set_id']}:{change_set['revision_set_id']}"
        # An abandonment is an audit record saying "we stopped waiting on this write", not
        # a submission. The closed attempt keeps its intent and its receipt, and this
        # attempt gets an intent of its own, chained off the one it supersedes: replaying
        # the same retry is idempotent, while the earlier account stays intact. Returning
        # the abandonment as the answer, or re-receipting its intent, both wedged the run.
        key = base_key
        abandoned = False
        for _ in range(8):
            completed = self.state.result_by_idempotency_key(key)
            response = completed["receipt"]["response"] if isinstance(completed, dict) else None
            if not _superseded_submit_receipt(response):
                break
            prior = self.state.intent_by_idempotency_key(key)
            if not isinstance(prior, dict):
                break
            abandoned = True
            key = f"{base_key}:after:{prior['intent_id']}"
        claim = self.state.claim_intent(self.run_id, "icode.submit", key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("SUBMIT_CONFLICT")
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
        intent = claim["intent"]
        cli = preflight["cli"]
        reconciliation = self._reconcile(cli, repo_path, change_set)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if reconciliation.get("reason_code") == "OK":
            return self._receipt(intent["intent_id"], change_set, reconciliation["change"], key)
        if reconciliation.get("reason_code") == "CR_IDENTITY_CONFLICT":
            return reconciliation
        if reconciliation.get("reason_code") != "CR_NOT_FOUND":
            return {**reconciliation, "intent_id": intent["intent_id"], "retry_allowed": False}
        # An existing intent means someone may already have pushed, so a blind re-push is
        # refused and the caller is sent to query instead. An *abandoned* lineage whose
        # reconcile says the CR does not exist is that query, already answered: the write
        # demonstrably did not land, so this attempt may proceed.
        if claim["status"] == "EXISTING" and not abandoned:
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        if self.submission_policy == "one_cr_per_repo":
            sibling = self._open_card_cr(cli, repo_path, change_set)
            blocked = self._profile_error()
            if blocked is not None:
                return blocked
            if isinstance(sibling, dict) and sibling.get("reason_code") == "CR_IDENTITY_CONFLICT":
                return {**sibling, "intent_id": intent["intent_id"], "retry_allowed": False}
            if isinstance(sibling, dict) and sibling.get("reason_code") == "CR_BASELINE_DRIFT":
                if change_set.get("submission_mode") == "create_new_cr":
                    sibling = None
                else:
                    return {
                        **sibling,
                        "intent_id": intent["intent_id"],
                        "retry_allowed": False,
                    }

        # Keep CR creation inside the iCode boundary. Current iCode supports worktrees
        # through --repo-path and emits the CR receipt that reconciliation consumes.
        # The reviewed commit is already HEAD-validated above; using push_cr therefore
        # sends exactly those reviewed bytes without a raw Git fallback.
        submit_repo = repo_path
        mirror = None
        try:
            submit_repo, mirror = self._materialize_cli_repository(repo_path, change_set)
            blocked = self._profile_error()
            if blocked is not None:
                return blocked
            pushed = _run(
                self.transport,
                [
                    cli, "git", "push_cr",
                    "--repo-path", str(submit_repo),
                    "--branch", change_set["target_branch"],
                ],
                cwd=submit_repo,
                timeout=600,
            )
        except Exception:
            return _failure("SUBMIT_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        finally:
            if mirror is not None:
                shutil.rmtree(str(mirror), ignore_errors=True)
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        if pushed["returncode"] != 0:
            failure = _failure(
                "SUBMIT_REJECTED",
                intent_id=intent["intent_id"],
                retry_allowed=False,
                **_bounded_cli_output(pushed),
            )
            self.state.receipt(intent["intent_id"], failure, [])
            return failure
        reconciliation = self._reconcile(cli, repo_path, change_set)
        if reconciliation.get("reason_code") == "OK":
            return self._receipt(intent["intent_id"], change_set, reconciliation["change"], key)
        if reconciliation.get("reason_code") == "CR_IDENTITY_CONFLICT":
            return reconciliation
        return _failure("SUBMIT_CONFIRMATION_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)

    def _materialize_cli_repository(
        self, repo_path: Path, change_set: dict[str, Any]
    ) -> tuple[Path, Path | None]:
        """Give iCode CLI a real clone when the owned input is a Git worktree.

        iCode CLI's repository check requires `.git` to be a directory. Git worktrees
        deliberately use a `.git` file, so passing the owned worktree directly fails
        before push_cr reaches Gerrit. The mirror contains only the already validated
        reviewed commit and is deleted after the CLI returns.
        """
        git_marker = repo_path / ".git"
        if git_marker.is_dir():
            return repo_path, None
        if not git_marker.is_file():
            raise RuntimeError("GIT_WORKTREE_INVALID")
        origin = _git_text(repo_path, "remote", "get-url", "origin")
        if not origin:
            return repo_path, None
        if not _icode_remote_url(origin):
            return repo_path, None
        mirror = Path(tempfile.mkdtemp(prefix="tom-autodev-icode-"))
        try:
            _run_git(repo_path, ["clone", "--no-local", str(repo_path), str(mirror)])
            _run_git(mirror, ["remote", "set-url", "origin", origin])
            _run_git(
                mirror,
                ["checkout", "-b", "tom-autodev-submit", change_set["commit_revision"]],
            )
            return mirror, mirror
        except Exception:
            shutil.rmtree(str(mirror), ignore_errors=True)
            raise

    def _remote_drift(
        self, repo_path: Path, change_set: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Refuse to push a change set whose base the target branch has left behind.

        A worktree is cut once, at WORKSPACE, and the branch keeps moving. Pushing a
        commit whose base is behind produces a CR that Gerrit immediately marks as
        conflicting, which reads like a defect in the change and costs a round trip to
        diagnose. The branch state is a fact the boundary can check before writing
        anything, so it does. Rebasing is deliberately not automatic: it rewrites bytes a
        Review already accepted, and how a conflict should be resolved is a judgement,
        not a mechanical step -- so the failure names the commits and files involved and
        stops there.
        """
        branch = change_set["target_branch"]
        origin = _git_text(repo_path, "remote", "get-url", "origin")
        if not origin:
            # No origin means there is no remote branch that could have moved. The push
            # will report the missing remote itself; inventing a drift failure here would
            # only mask that.
            return None
        fetched = _git_text(repo_path, "fetch", "origin", branch)
        if fetched is None:
            if _icode_remote_url(origin):
                # An iCode URL that cannot be fetched from this control plane is not
                # evidence the branch moved. push_cr still talks to that remote; treating
                # the failed probe as REMOTE_STATE_UNKNOWN blocked every real submission
                # whose origin was copied onto a temporary mirror.
                return None
            return _failure("REMOTE_STATE_UNKNOWN")
        tip = _git_text(repo_path, "rev-parse", "FETCH_HEAD")
        if not tip:
            return _failure("REMOTE_STATE_UNKNOWN")
        base = _git_text(repo_path, "merge-base", change_set["commit_revision"], tip)
        if not base:
            return _failure("REMOTE_STATE_UNKNOWN")
        if base == tip:
            return None
        missing = _git_text(repo_path, "rev-list", "--count", f"{base}..{tip}") or "?"
        touched = _git_text(repo_path, "diff", "--name-only", f"{base}..{tip}") or ""
        ours = set((_git_text(repo_path, "diff", "--name-only", f"{base}..{change_set['commit_revision']}") or "").splitlines())
        overlap = sorted(set(touched.splitlines()) & ours)
        return _failure(
            "BASELINE_BEHIND_REMOTE",
            target_branch=branch,
            remote_tip=tip,
            merge_base=base,
            commits_behind=missing,
            conflicting_paths=overlap[:20],
            retry_allowed=False,
        )

    def _open_card_cr(
        self, cli: str, repo_path: Path, change_set: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Allow an identity-matching open CR to receive the next patchset.

        Reconcile has already established that no CR carries *this* change set, so what is
        left is a sibling: another change set of the same requirement, still open in the
        same repository. The commit already carries the CR's Change-Id, so push_cr appends
        the reviewed patchset. Only a different branch/owner/module identity is unsafe.
        """
        try:
            result = _run(
                self.transport,
                [cli, "api", "get_repo_reviews", "--repo", change_set["module"], "--status", "NEW", "-o", "json"],
                cwd=repo_path,
                timeout=30,
            )
        except Exception:
            return _failure("CR_QUERY_FAILED")
        if result["returncode"] != 0:
            return _failure("CR_QUERY_FAILED")
        try:
            changes = json.loads(result["stdout"]).get("data", {}).get("changes", [])
        except (AttributeError, json.JSONDecodeError):
            return _failure("CR_RESPONSE_INVALID")
        if not isinstance(changes, list):
            return _failure("CR_RESPONSE_INVALID")
        change_id = _commit_change_id(repo_path, change_set["commit_revision"])
        for change in changes:
            if not isinstance(change, dict):
                continue
            owner = change.get("owner") if isinstance(change.get("owner"), dict) else {}
            if (
                _card_subject_matches(change.get("subject"), change_set["card_id"])
                and _same_account(owner.get("username"), change_set["owner"])
            ):
                if (
                    change.get("branch") not in (None, change_set["target_branch"])
                    or change.get("project") not in (None, change_set["module"])
                ):
                    return _failure(
                        "CR_IDENTITY_CONFLICT",
                        module=change_set["module"],
                        card_id=change_set["card_id"],
                        change_number=str(change.get("_number") or change.get("number") or ""),
                    )
                if change_id and change.get("change_id") == change_id:
                    return {"reason_code": "CR_ALREADY_MATCHED", "change_number": str(change.get("_number") or change.get("number") or "")}
                current = change.get("current_revision")
                if (
                    isinstance(current, str)
                    and current
                    and not _is_ancestor(repo_path, current, change_set["commit_revision"])
                ):
                    return _failure(
                        "CR_BASELINE_DRIFT",
                        module=change_set["module"],
                        card_id=change_set["card_id"],
                        change_number=str(change.get("_number") or change.get("number") or ""),
                        cr_revision=current,
                        submitted_revision=change_set["commit_revision"],
                    )
                return {"reason_code": "CR_APPEND_ALLOWED", "change_number": str(change.get("_number") or change.get("number") or "")}
        return None

    def _discover_cli(self, repo_path: Path) -> dict[str, Any]:
        found = False
        for candidate in self.binary_candidates:
            if not candidate:
                continue
            resolved = self.executable_resolver(os.path.expanduser(candidate))
            if not resolved:
                continue
            found = True
            try:
                help_result = _run(self.transport, [resolved, "--help"], cwd=repo_path, timeout=30)
            except Exception:
                continue
            if help_result["returncode"] != 0:
                continue
            commands = _top_level_commands(help_result["stdout"])
            if not set(_REQUIRED_COMMANDS).issubset(commands):
                continue
            return {"ok": True, "reason_code": "OK", "cli": resolved}
        return _failure("ICODE_SUBCOMMAND_MISSING" if found else "ICODE_CLI_NOT_FOUND")

    def _validate_change_set(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return _failure("CHANGE_SET_INVALID")
        required_strings = (
            "run_id", "change_set_id", "revision_set_id", "input_hash", "repo_path", "module",
            "target_branch", "commit_revision", "card_id", "owner",
        )
        if any(not _nonempty(value.get(field)) for field in required_strings):
            return _failure("CHANGE_SET_INVALID")
        if value["run_id"] != self.run_id:
            return _failure("CHANGE_SET_RUN_MISMATCH")
        revisions = value.get("revision_set")
        if not isinstance(revisions, dict):
            return _failure("REVISION_SET_INVALID")
        business = revisions.get("business")
        test = revisions.get("test")
        if not all(isinstance(item, dict) for item in (business, test)):
            return _failure("REVISION_SET_INVALID")
        # Which entry the submission itself is about: a task that only touches the test
        # repository submits that repository, and checking `module`/`commit_revision`
        # against the business entry regardless rejected exactly those submissions. The
        # role is derived rather than carried, so the canonical descriptor bytes that the
        # reviewed artifact pins stay unchanged.
        if not any(
            entry.get("module") == value["module"]
            and entry.get("revision") == value["commit_revision"]
            for entry in (business, test)
        ):
            return _failure("REVISION_SET_MISMATCH")
        for item in (business, test):
            if any(not _nonempty(item.get(field)) for field in ("module", "revision", "branch")):
                return _failure("REVISION_SET_INVALID")
        artifact_id = value.get("reviewed_artifact_id")
        if not _nonempty(artifact_id):
            return _failure("CHANGE_SET_REVIEW_REQUIRED")
        reviewed = self.artifacts.get(artifact_id)
        if (
            not reviewed.get("valid")
            or reviewed.get("run_id") != self.run_id
            or reviewed.get("kind") != "change-set"
            or reviewed.get("metadata", {}).get("verdict") != "PASS"
            or reviewed.get("metadata", {}).get("revision_set_id") != value["revision_set_id"]
        ):
            return _failure("CHANGE_SET_REVIEW_REQUIRED")
        canonical = {
            field: value[field]
            for field in (
                "run_id", "change_set_id", "revision_set_id", "repo_path", "module",
                "target_branch", "commit_revision", "card_id", "owner", "revision_set",
            )
            if field in value
        }
        if "submission_mode" in value:
            canonical["submission_mode"] = value["submission_mode"]
        canonical_bytes = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if reviewed.get("content") != canonical_bytes:
            return _failure("REVIEW_ARTIFACT_MISMATCH")
        canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
        if reviewed.get("sha256") != canonical_hash or value["input_hash"] != canonical_hash:
            return _failure("INPUT_HASH_MISMATCH")
        return None

    def _reconcile(self, cli: str, repo_path: Path, change_set: dict[str, Any]) -> dict[str, Any]:
        try:
            result = _run(
                self.transport,
                [cli, "api", "get_repo_reviews", "--repo", change_set["module"], "--status", "NEW", "-o", "json"],
                cwd=repo_path,
                timeout=30,
            )
        except Exception:
            return _failure("CR_QUERY_FAILED")
        if result["returncode"] != 0:
            return _failure("CR_QUERY_FAILED")
        try:
            decoded = json.loads(result["stdout"])
            changes = decoded.get("data", {}).get("changes", [])
        except (AttributeError, json.JSONDecodeError):
            return _failure("CR_RESPONSE_INVALID")
        if not isinstance(changes, list):
            return _failure("CR_RESPONSE_INVALID")
        related = []
        change_id = _commit_change_id(repo_path, change_set["commit_revision"])
        for change in changes:
            if not isinstance(change, dict):
                continue
            # What makes an open CR *this* change set is its Gerrit identity: the commit
            # it currently points at, or the Change-Id the commit carries when a new
            # patchset is being appended. Matching on the card prefix instead declared
            # every later submission an identity conflict against an earlier sibling,
            # and a requirement that spans tasks legitimately has several CRs in one
            # repository -- the product cases for one task and the retired cases for
            # another.
            if change.get("current_revision") == change_set["commit_revision"] or (
                change_id is not None and change.get("change_id") == change_id
            ):
                related.append(change)
        for change in related:
            if _exact_change(change, change_set):
                return {"ok": True, "reason_code": "OK", "change": change}
        if related:
            return _failure("CR_IDENTITY_CONFLICT")
        return _failure("CR_NOT_FOUND")

    def _receipt(
        self,
        intent_id: str,
        change_set: dict[str, Any],
        change: dict[str, Any],
        submit_key: str,
    ) -> dict[str, Any]:
        blocked = self._profile_error()
        if blocked is not None:
            return blocked
        number = str(change.get("_number") or change.get("number") or "")
        patchset = str(change.get("current_revision") or "")
        url = change.get("url") or change.get("change_url") or _cr_url(number)
        declared_module = change.get("module") or change.get("project") or change.get("repo")
        if (
            not number.isdigit()
            or patchset != change_set["commit_revision"]
            or (declared_module is not None and declared_module != change_set["module"])
            or not _valid_url(url, number)
        ):
            return _failure("CR_RECEIPT_INVALID", intent_id=intent_id, retry_allowed=False)
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "change_set_id": change_set["change_set_id"],
            "revision_set_id": change_set["revision_set_id"],
            "repo_path": str(Path(change_set["repo_path"]).resolve()),
            "module": change_set["module"],
            "target_branch": change_set["target_branch"],
            "commit_revision": change_set["commit_revision"],
            "revision_set": change_set["revision_set"],
            "change_number": number,
            "patchset": patchset,
            # Which intent lineage receipted this submission. An attempt that supersedes
            # an abandoned one lives under a chained key, and the caller has to be able
            # to find the durable record without re-deriving that chain.
            "submit_key": submit_key,
            "cr_url": url,
            "evidence_refs": [
                f"icode-cr-{number}",
                *[
                    f"revision-{entry['revision']}"
                    for entry in change_set["revision_set"].values()
                ],
            ],
        }
        self.state.receipt(intent_id, response, response["evidence_refs"])
        return response


def _default_candidates() -> list[str]:
    return [
        os.environ.get("ICODE_CLI_PATH", ""),
        "icode",
        str(Path.home() / ".icode" / "bin" / "icode"),
        "icode-cli",
        str(Path.home() / ".icode" / "bin" / "icode-cli"),
    ]


def _resolve_executable(value: str) -> str | None:
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or "/" in value:
        return str(expanded) if expanded.is_file() and os.access(expanded, os.X_OK) else None
    return shutil.which(value)


def _git(repo_path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *arguments], capture_output=True, text=True,
        timeout=10, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("GIT_QUERY_FAILED")
    return result.stdout.strip()


def _run_git(repo_path: Path, arguments: list[str]) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("GIT_MATERIALIZATION_FAILED")


def _run(transport: Any, argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    result = transport.run(argv, cwd=cwd, timeout=timeout)
    if isinstance(result, dict):
        code = result.get("returncode", result.get("exit_code", 0))
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
    else:
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    return {"returncode": int(code), "stdout": str(stdout or ""), "stderr": str(stderr or "")}


def _top_level_commands(help_text: str) -> set[str]:
    return {
        match.group(1)
        for line in help_text.splitlines()
        if (match := re.match(r"^\s{0,4}([A-Za-z][A-Za-z0-9_-]*)\b", line)) is not None
    }


def _approved_record(
    ledger: ApprovalLedger, supplied: Any, *, run_id: str, action: str, input_hash: str
) -> dict[str, Any] | None:
    if not isinstance(supplied, dict) or not _nonempty(supplied.get("approval_id")):
        return _failure("APPROVAL_REQUIRED")
    record = ledger.get(supplied["approval_id"])
    if record is None:
        return _failure("APPROVAL_REQUIRED")
    if record.get("run_id") != run_id or record.get("run_id") == "legacy":
        return _failure("APPROVAL_RUN_MISMATCH")
    if gate_of(record.get("action")) != action:
        return _failure("APPROVAL_GATE_MISMATCH")
    if supplied.get("input_hash") != input_hash or record.get("input_hash") != input_hash:
        return _failure("APPROVAL_INPUT_MISMATCH")
    if record.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED")
    return None


def _git_text(repo_path: Any, *args: str) -> str | None:
    """Run a read-only git command in a repository, or report that it could not run."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _is_ancestor(repo_path: Path, ancestor: str, revision: str) -> bool:
    """Check CR baseline ancestry without rewriting or mutating the worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", ancestor, revision],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_OUTPUT_LIMIT = 2000
_SECRET_LINE = re.compile(r"(?i)(token|password|secret|authorization)\s*[:=]")


def _icode_remote_url(value: str) -> bool:
    """Whether a git remote is an iCode URL, not a local path.

    Cloning a worktree with `--no-local` copies objects, but `origin` becomes the
    worktree path. iCode CLI then fails with `failed to extract repo name from
    remote URL` and never reaches Gerrit.
    """
    parsed = urlparse(value)
    if parsed.scheme in {"ssh", "git", "http", "https"}:
        return bool(parsed.netloc)
    if parsed.scheme == "" and value.startswith("ssh://"):
        return True
    if "icode.baidu.com" in value and "://" in value:
        return True
    return False


def _bounded_text(value: Any, limit: int = _OUTPUT_LIMIT) -> str:
    text = str(value or "").replace("\x00", "")
    lines = [line for line in text.splitlines() if not _SECRET_LINE.search(line)]
    clipped = "\n".join(lines)
    return clipped[:limit]


def _bounded_cli_output(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "cli_exit_code": result.get("returncode"),
        "cli_stdout": _bounded_text(result.get("stdout")),
        "cli_stderr": _bounded_text(result.get("stderr")),
    }


def _unpushable_commit(repo_path: Path, change_set: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse commits iCode will reject, before claiming a write intent.

    Two local facts have already failed this run's push: a missing Change-Id trailer,
    and a committer that is not the pushing account. Both are visible from `git log`
    and do not require a remote write to diagnose.
    """
    revision = change_set["commit_revision"]
    if _commit_change_id(repo_path, revision) is None:
        return _failure("CHANGE_ID_MISSING", commit_revision=revision, retry_allowed=False)
    committer = _git_text(repo_path, "log", "-1", "--format=%ce", revision) or ""
    owner = str(change_set.get("owner") or "").split("@", 1)[0].strip()
    local = committer.split("@", 1)[0].strip()
    if not local or local != owner:
        return _failure(
            "GIT_IDENTITY_MISMATCH",
            commit_revision=revision,
            committer=committer,
            owner=change_set.get("owner"),
            retry_allowed=False,
        )
    return None


def _commit_change_id(repo_path: Any, revision: str) -> str | None:
    """The Change-Id trailer of a commit, which is the CR's durable identity.

    Read from the worktree rather than carried in the descriptor: the descriptor's
    canonical bytes are pinned by the reviewed artifact, and the trailer is already in
    the commit the descriptor names.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "log", "-1", "--format=%B", revision],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in reversed(completed.stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Change-Id:"):
            value = stripped.split(":", 1)[1].strip()
            return value or None
    return None


def _exact_change(change: dict[str, Any], expected: dict[str, Any]) -> bool:
    owner = change.get("owner") if isinstance(change.get("owner"), dict) else {}
    return (
        change.get("current_revision") == expected["commit_revision"]
        and change.get("branch") == expected["target_branch"]
        and _same_account(owner.get("username"), expected["owner"])
        and _card_subject_matches(change.get("subject"), expected["card_id"])
    )


def _same_account(reported: Any, expected: Any) -> bool:
    """Whether two spellings of one account match.

    iCode reports an owner as a uuap username (`zhaotao02`) while a profile spells
    people as addresses (`zhaotao02@baidu.com`). Comparing them literally can never
    succeed, which made the reconcile path unable to recognise a CR this run had
    already pushed and report CR_IDENTITY_CONFLICT against its own work.
    """
    if not isinstance(reported, str) or not isinstance(expected, str):
        return False
    return reported.split("@", 1)[0].strip() == expected.split("@", 1)[0].strip() != ""


def _card_subject_matches(subject: Any, card_id: Any) -> bool:
    """Match a card token without treating BGW-1 as a prefix of BGW-10."""
    if not isinstance(subject, str) or not isinstance(card_id, str) or not card_id.strip():
        return False
    subject = subject.strip()
    card_id = card_id.strip()
    return subject == card_id or subject.startswith(card_id + " ") or subject.startswith(card_id + ":")


def _cr_url(change_number: str) -> str:
    """The canonical review URL for a change.

    `get_repo_reviews` does not return one, and the receipt has to carry a link a
    person can open, so it is derived from the change number rather than left empty —
    an absent URL failed `_valid_url` and rejected an otherwise valid receipt.
    """
    return f"http://icode.baidu.com/myreview/changes/{change_number}" if change_number.isdigit() else ""


def _valid_url(value: Any, change_number: str) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and change_number in [component for component in parsed.path.split("/") if component]
    )


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _superseded_submit_receipt(response: Any) -> bool:
    from state_store import _superseded_external_receipt

    return _superseded_external_receipt(response)


def _failure(reason_code: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **fields}
