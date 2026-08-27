from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from approval_ledger import ApprovalLedger
from artifact_store import ArtifactStore
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
        self._cli_by_repo: dict[str, str] = {}

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
            or Path(str(ownership.get("worktree_path"))).resolve() != path
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
        if revision != binding.get("baseline_revision"):
            return _failure("STALE_BASELINE", revision=revision)
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

    def submit(self, change_set: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
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
        if preflight.get("reason_code") != "OK":
            return preflight
        if preflight["module"] != change_set["module"] or preflight["target_branch"] != change_set["target_branch"]:
            return _failure("CHANGE_SET_WORKTREE_MISMATCH")
        if preflight["revision"] != change_set["commit_revision"]:
            return _failure("STALE_BASELINE")

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
        key = f"icode.submit:{self.run_id}:{change_set['change_set_id']}:{change_set['revision_set_id']}"
        claim = self.state.claim_intent(self.run_id, "icode.submit", key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("SUBMIT_CONFLICT")
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
        intent = claim["intent"]
        cli = preflight["cli"]
        reconciliation = self._reconcile(cli, repo_path, change_set)
        if reconciliation.get("reason_code") == "OK":
            return self._receipt(intent["intent_id"], change_set, reconciliation["change"])
        if reconciliation.get("reason_code") == "CR_IDENTITY_CONFLICT":
            return reconciliation
        if reconciliation.get("reason_code") != "CR_NOT_FOUND":
            return {**reconciliation, "intent_id": intent["intent_id"], "retry_allowed": False}
        if claim["status"] == "EXISTING":
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)

        try:
            pushed = _run(
                self.transport,
                [cli, "git", "push_cr", "--branch", change_set["target_branch"]],
                cwd=repo_path,
                timeout=600,
            )
        except Exception:
            return _failure("SUBMIT_RESULT_UNKNOWN", intent_id=intent["intent_id"], retry_allowed=False)
        if pushed["returncode"] != 0:
            return _failure("SUBMIT_REJECTED", intent_id=intent["intent_id"], retry_allowed=False)
        reconciliation = self._reconcile(cli, repo_path, change_set)
        if reconciliation.get("reason_code") == "OK":
            return self._receipt(intent["intent_id"], change_set, reconciliation["change"])
        if reconciliation.get("reason_code") == "CR_IDENTITY_CONFLICT":
            return reconciliation
        return _failure("SUBMIT_CONFIRMATION_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)

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
        if business.get("module") != value["module"] or business.get("revision") != value["commit_revision"]:
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
        }
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
        for change in changes:
            if not isinstance(change, dict):
                continue
            subject = str(change.get("subject") or "")
            if subject.startswith(change_set["card_id"]) or change.get("current_revision") == change_set["commit_revision"]:
                related.append(change)
        for change in related:
            if _exact_change(change, change_set):
                return {"ok": True, "reason_code": "OK", "change": change}
        if related:
            return _failure("CR_IDENTITY_CONFLICT")
        return _failure("CR_NOT_FOUND")

    def _receipt(self, intent_id: str, change_set: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
        number = str(change.get("_number") or change.get("number") or "")
        patchset = str(change.get("current_revision") or "")
        url = change.get("url") or change.get("change_url")
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
    if record.get("action") != action:
        return _failure("APPROVAL_GATE_MISMATCH")
    if supplied.get("input_hash") != input_hash or record.get("input_hash") != input_hash:
        return _failure("APPROVAL_INPUT_MISMATCH")
    if record.get("effective_decision") != "APPROVE":
        return _failure("APPROVAL_REQUIRED")
    return None


def _exact_change(change: dict[str, Any], expected: dict[str, Any]) -> bool:
    owner = change.get("owner") if isinstance(change.get("owner"), dict) else {}
    return (
        change.get("current_revision") == expected["commit_revision"]
        and change.get("branch") == expected["target_branch"]
        and owner.get("username") == expected["owner"]
        and str(change.get("subject") or "").startswith(expected["card_id"])
    )


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


def _failure(reason_code: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **fields}
