"""Post-release, human-approved knowledge CRs against the run's workspace pin.

Planning is read-only. Only the process that creates the write intent may push;
recovery of an unanswered intent is a Change-Id lookup, never another push.
This approval action deliberately lives outside the pinned workflow's G gates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import intent_executor
import workspace_knowledge
from approval_ledger import gate_of
from cli_transport import ProcessTransport
from clients.icode_runtime import (
    _bounded_cli_output, _cr_url, _push_refused, _run, _single_push_target, _valid_url,
)
from execution_guard import execution_guard, guard_execution
from phase_protocol import _approval_input_hash
from schema_validator import validate_knowledge_candidates


ACTION = "KNOWLEDGE_WRITEBACK"
_OPERATION = "workspace.writeback"
_PUSH_TIMEOUT = 600


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def material_key(run_id: str, input_hash: str) -> str:
    return f"workspace.writeback-material:{run_id}:{input_hash}"


def _intent_key(run_id: str, input_hash: str) -> str:
    return f"workspace.writeback:{run_id}:{input_hash}"


def _failure(reason_code: str, run_id: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "run_id": run_id, **details}


def _git(repo: Path, *arguments: str, input_text: str | None = None) -> str:
    # Neither checkout nor commit may execute the user's hooks. In particular the
    # deterministic Change-Id below must not be replaced by a commit-msg hook.
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
        input=input_text.encode("utf-8") if input_text is not None else None,
        check=True, capture_output=True, timeout=120,
    ).stdout.decode("utf-8")


def plan(orchestrator: Any, run_id: str) -> dict[str, Any]:
    """Collect only integrity-checked, exactly G6-approved diagnosis candidates."""
    events = orchestrator.state.events(run_id)
    if not events or events[-1].get("state") != "RELEASE_SUCCESS":
        return _failure("WRITEBACK_RUN_NOT_RELEASED", run_id)
    pin = workspace_knowledge.pinned(events)
    if not pin or not all(isinstance(pin.get(k), str) and pin[k].strip()
                          for k in ("repository", "path", "commit")):
        return _failure("WRITEBACK_NO_WORKSPACE", run_id)
    unavailable = workspace_knowledge.verify(pin)
    if unavailable:
        return _failure(unavailable, run_id)

    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for artifact in orchestrator.artifacts.phase_artifacts(run_id, "DIAGNOSE"):
        envelope = artifact.get("envelope")
        if not artifact.get("valid") or not isinstance(envelope, dict):
            continue
        if envelope.get("run_id") != run_id or envelope.get("phase") != "DIAGNOSE":
            continue
        approval = orchestrator.approvals.get(envelope.get("approval_id"))
        content = envelope.get("content")
        if (not isinstance(approval, dict) or not isinstance(content, dict)
                or approval.get("run_id") != run_id
                or gate_of(approval.get("action")) != "G6"
                or approval.get("effective_decision") != "APPROVE"
                or approval.get("input_hash") != envelope.get("approval_input_hash")
                or _hash(content) != envelope.get("content_hash")
                or envelope.get("approval_input_hash") != _approval_input_hash(
                    envelope, envelope["content_hash"], envelope.get("source_revisions"))):
            continue
        raw = content.get("knowledge_candidates", [])
        issues = validate_knowledge_candidates(raw)
        if issues:
            return _failure(
                "WRITEBACK_CANDIDATE_INVALID", run_id,
                source_artifact_id=artifact["artifact_id"],
                schema_issues=[{"path": issue.path, "kind": issue.kind} for issue in issues],
            )
        for candidate in raw:
            identity = (candidate["target"], candidate["body"])
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append({
                **candidate,
                "body_sha256": hashlib.sha256(candidate["body"].encode("utf-8")).hexdigest(),
                "source_artifact_id": artifact["artifact_id"],
            })

    try:
        repo = Path(pin["path"]).expanduser()
        # A missing file is not an unreadable git show: distinguish the two rather
        # than silently including candidates when the object database is broken.
        files = set(_git(repo, "ls-tree", "-r", "--name-only", "-z", pin["commit"]).split("\0")) - {""}
        conflict = _case_conflict(files, [item["target"] for item in candidates])
        if conflict is not None:
            return _failure(
                "WRITEBACK_CANDIDATE_INVALID", run_id, target=conflict,
                schema_issues=[{"path": "knowledge_candidates.target", "kind": "target_case_conflict"}],
            )
        existing = {
            target: _git(repo, "show", f"{pin['commit']}:{target}")
            for target in sorted({item["target"] for item in candidates}) if target in files
        }
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _failure("WRITEBACK_WORKSPACE_READ_FAILED", run_id)
    candidates = [item for item in candidates
                  if item["body"] not in existing.get(item["target"], "")]
    if not candidates:
        return {"ok": True, "reason_code": "WRITEBACK_NOTHING", "run_id": run_id}
    candidates.sort(key=lambda item: (item["target"], item["body"], item["source_artifact_id"]))
    proposal = {"run_id": run_id, "repository": pin["repository"],
                "commit": pin["commit"], "candidates": candidates}
    return {"ok": True, "reason_code": "WRITEBACK_READY", "proposal": proposal,
            "input_hash": _hash(proposal)}


def _case_conflict(files: set[str], targets: list[str]) -> str | None:
    """A target that a case-insensitive checkout would fold onto another tracked path.

    The file (or any parent directory) may exist only under a different spelling,
    e.g. docs/readme.md vs README.md: the write would land in the other path and the
    deduplication above, which compares literally, would not see it (R12B-002).
    """
    spellings: dict[str, set[str]] = {}
    for name in files:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            spellings.setdefault(prefix.lower(), set()).add(prefix)
    for target in sorted(set(targets)):
        parts = target.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            known = spellings.get(prefix.lower())
            if known and prefix not in known:
                return target
    return None


def _published(repo: Path, commit: str) -> bool:
    """Whether the pin is already on the reviewed branch (origin/master), read locally."""
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "merge-base",
             "--is-ancestor", commit, "refs/remotes/origin/master"],
            capture_output=True, timeout=120,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@guard_execution
def request(
    orchestrator: Any, run_id: str, *, comate_client: Any = None,
    infoflow_client: Any = None, deadline_at: str | None = None,
) -> dict[str, Any]:
    """Pin the full proposal and ask the profile's members; never auto-authorize."""
    planned = plan(orchestrator, run_id)
    if planned.get("reason_code") != "WRITEBACK_READY":
        return planned
    loaded = orchestrator._runtime_profile(run_id)
    if not loaded.get("ok"):
        return loaded
    input_hash = planned["input_hash"]
    orchestrator.state.save_idempotency_result(material_key(run_id, input_hash), planned["proposal"])
    from approval_delivery import ComateApprovalClient
    from orchestrator import _approval_context, _infoflow_approval_client

    context = _approval_context(orchestrator, run_id, action=ACTION, input_hash=input_hash)
    if "error" in context:
        return context["error"]
    if comate_client is None:
        comate_client = ComateApprovalClient()
    if infoflow_client is None:
        infoflow_client = _infoflow_approval_client(orchestrator)
    result = orchestrator.request_infoflow_approval(
        run_id, ACTION, input_hash, member_policy=context["member_policy"],
        deadline_at=deadline_at, evidence=context["evidence"],
        comate_client=comate_client, infoflow_client=infoflow_client,
    )
    return {**planned, **result, "ok": result.get("reason_code") in (None, "OK"),
            "reason_code": result.get("reason_code") or "OK"}


class _LocalError(Exception):
    pass


def _safe_file(root: Path, relative: str) -> Path:
    """Reject symlinks, including parents, even when they point inside the tree.

    Lexically confined targets can still escape via a tracked docs/ symlink. The
    index and root changelog need the same protection as the candidate itself.
    """
    path = root / relative
    if not path.is_relative_to(root):
        raise _LocalError("WRITEBACK_UNSAFE_PATH")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise _LocalError("WRITEBACK_UNSAFE_PATH")
        if current.exists() and current != path and not current.is_dir():
            raise _LocalError("WRITEBACK_UNSAFE_PATH")
    if path.exists() and not path.is_file():
        raise _LocalError("WRITEBACK_UNSAFE_PATH")
    return path


def _append(text: str, section: str) -> str:
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + separator + section + ("" if section.endswith("\n") else "\n")


def _index_entry(index: Path, title: str, filename: str) -> None:
    text = index.read_bytes().decode("utf-8") if index.exists() else ""
    label = " ".join(title.splitlines()).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    entry = f"- [{label}]({filename})\n"
    heading = re.search(r"(?im)^(#{1,6})[ \t]+Knowledge Map[ \t]*\r?$", text)
    if heading:
        next_heading = re.search(r"(?m)^#{1," + str(len(heading[1])) + r"}[ \t]+", text[heading.end():])
        end = heading.end() + next_heading.start() if next_heading else len(text)
        if re.search(r"\]\(" + re.escape(filename) + r"\)", text[heading.end():end]):
            return
        text = _append(text[:end], entry) + ("\n" + text[end:] if end < len(text) else "")
    else:
        text = _append(text, "## Knowledge Map\n\n" + entry)
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(text, encoding="utf-8")


def _write_candidates(worktree: Path, proposal: dict[str, Any], card_id: str) -> list[str]:
    candidates = proposal["candidates"]
    targets = {item["target"]: _safe_file(worktree, item["target"]) for item in candidates}
    indexes = {str(Path(target).parent / "index.md") for target in targets if not targets[target].exists()}
    for index in indexes:
        _safe_file(worktree, index)
    changelog = _safe_file(worktree, "CHANGELOG.md")
    changed = set(targets) | indexes
    for item in candidates:
        target = targets[item["target"]]
        existed = target.exists()
        previous = target.read_bytes().decode("utf-8") if existed else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        heading = "##" if existed else "#"
        target.write_text(_append(previous, f"{heading} {item['title']}\n\n{item['body']}"), encoding="utf-8")
        if not existed:
            _index_entry(target.parent / "index.md", item["title"], target.name)
    if changelog.exists():
        day = datetime.now().astimezone().date().isoformat()
        entry = (f"## {day}\n\n- {card_id}：tom-autodev 运行 {proposal['run_id'][:12]} 写回知识\n"
                 + "".join(f"  - {item['target']}：{item['title']}\n" for item in candidates) + "\n")
        changelog.write_text(entry + changelog.read_bytes().decode("utf-8"), encoding="utf-8")
        changed.add("CHANGELOG.md")
    return sorted(changed)


def _prepare_cli_worktree(worktree: Path) -> None:
    """Expose a directory-shaped .git without cloning or changing remote config.

    The iCode CLI's repository precheck requires a .git directory (the business
    adapter uses a mirror clone for this). Keep this operation on the requested
    temporary worktree instead: the symlink targets its own gitdir, not the common
    gitdir. execute restores the saved gitfile before removing the worktree.
    """
    git_dir = _git(worktree, "rev-parse", "--absolute-git-dir").strip()
    binding = ("rev-parse", "HEAD", "HEAD^", "--show-toplevel", "--git-common-dir")
    before = _git(worktree, *binding)
    remotes = _git(worktree, "remote").splitlines()
    push_urls = {remote: _git(worktree, "remote", "get-url", "--push", "--all", remote)
                 for remote in remotes}
    marker = worktree / ".git"
    marker.unlink()
    marker.symlink_to(git_dir, target_is_directory=True)
    if (not marker.is_dir() or _git(worktree, *binding) != before
            or _git(worktree, "remote").splitlines() != remotes
            or any(_git(worktree, "remote", "get-url", "--push", "--all", name) != urls
                   for name, urls in push_urls.items())):
        raise _LocalError("WRITEBACK_CLI_WORKTREE_INVALID")


def _message(card_id: str, proposal: dict[str, Any], change_id: str) -> str:
    lines = [f"{card_id} docs: tom-autodev 运行 {proposal['run_id'][:12]} 写回知识", ""]
    for item in proposal["candidates"]:
        lines += [f"- {item['target']}: {item['title']}",
                  f"  body_sha256: {item['body_sha256']}", f"  reason: {item['reason']}"]
    return "\n".join([*lines, "", f"Change-Id: {change_id}", ""])


def _pushed_url(output: str) -> tuple[str, str] | None:
    found: dict[str, str] = {}
    for raw in re.findall(r"https?://[^\s<>\"'\x1b]+", output):
        url = raw.rstrip(").,;，。；")
        try:
            parsed = urlparse(url)
            # push_cr prints the review on the console host (seen 2026-11-02 for the
            # BGW-2020 CR: console.cloud.baidu-int.com/devops/icode/myreview/changes/c/...).
            if parsed.hostname not in {"icode.baidu.com", "console.cloud.baidu-int.com"}:
                continue
        except ValueError:
            continue
        number = re.search(r"/(?:changes/|reviews/|\+/)([0-9]+)(?:/|$)", parsed.path)
        if number is None:
            number = re.fullmatch(r"/([0-9]+)/?", parsed.path)
        if number and _valid_url(url, number[1]):
            found.setdefault(number[1], url)
    return next(iter(found.items())) if len(found) == 1 else None


def _answer(orchestrator: Any, intent: dict[str, Any], verdict: intent_executor.Verdict) -> dict[str, Any]:
    return {"run_id": intent["run_id"], **intent_executor.answer(
        orchestrator.state, intent, verdict, unknown_reason="WRITEBACK_RESULT_UNKNOWN",
        ambiguous_reason="WRITEBACK_IDENTITY_AMBIGUOUS",
    )}


def _receipt(
    orchestrator: Any, intent: dict[str, Any], number: str, url: str,
    commit_revision: str | None = None,
) -> dict[str, Any]:
    payload = intent["payload"]
    result = {
        "ok": True, "reason_code": "WRITEBACK_PUSHED", "run_id": intent["run_id"],
        "input_hash": payload["input_hash"], "approval_id": payload["approval_id"],
        "intent_id": intent["intent_id"], "repository": payload["repository"],
        "branch": payload["branch"], "target_branch": "master", "change_id": payload["change_id"],
        "change_number": number, "cr_url": url, "commit_revision": commit_revision,
        "evidence_refs": [f"icode-cr-{number}"],
    }
    written = orchestrator.state.receipt_unless_ambiguous(
        intent["intent_id"], result, result["evidence_refs"],
    )
    if written is None:
        return _answer(orchestrator, intent, intent_executor.Verdict.ambiguous())
    return written["response"]


def _observe(orchestrator: Any, intent: dict[str, Any], transport: Any, binary: str) -> dict[str, Any]:
    sticky = intent_executor.sticky_ambiguous(orchestrator.state, intent)
    if sticky is not None:
        return _answer(orchestrator, intent, sticky)
    payload = intent["payload"]
    try:
        result = _run(
            transport,
            [binary, "api", "get_repo_reviews", "--repo", payload["repository"], "--status", "NEW", "-o", "json"],
            cwd=Path(payload["path"]), timeout=30,
        )
        decoded = json.loads(result["stdout"]) if result["returncode"] == 0 else None
        changes = decoded["data"]["changes"]
        if not isinstance(changes, list) or any(not isinstance(change, dict) for change in changes):
            raise ValueError("INVALID_REVIEW_LIST")
    except Exception:  # a failed/invalid read is not proof that the push did not land
        return _answer(orchestrator, intent, intent_executor.Verdict.unknown())
    matches = [change for change in changes if change.get("change_id") == payload["change_id"]]
    if not matches:
        # NEW alone cannot prove absence (nor can an empty, possibly lagging list).
        return _answer(orchestrator, intent, intent_executor.Verdict.unknown())
    observations = orchestrator.state.external_observations(intent["run_id"], intent_id=intent["intent_id"])
    local_revision = next((item["observation"].get("commit_revision") for item in reversed(observations)
                           if item["observation"].get("operation") == "workspace.writeback.commit"), None)
    change = matches[0]
    declared = change.get("project") or change.get("module") or change.get("repo")
    if (len(matches) != 1 or change.get("branch") not in (None, "", "master")
            or declared not in (None, "", payload["repository"])
            or (local_revision and change.get("current_revision") not in (None, "", local_revision))):
        return _answer(orchestrator, intent, intent_executor.Verdict.ambiguous(
            candidates=sorted(str(item.get("_number") or item.get("number") or "") for item in matches)))
    number = str(change.get("_number") or change.get("number") or "")
    url = change.get("url") or change.get("change_url") or _cr_url(number)
    try:
        valid = number.isdigit() and _valid_url(url, number)
    except ValueError:
        valid = False
    if not valid:
        return _answer(orchestrator, intent, intent_executor.Verdict.unknown())
    return _receipt(orchestrator, intent, number, url, local_revision or change.get("current_revision"))


def _local_failure(orchestrator: Any, intent: dict[str, Any], reason: str) -> dict[str, Any]:
    result = _failure(reason, intent["run_id"], intent_id=intent["intent_id"],
                      abandoned=True, local_error_proven=True, retry_allowed=False,
                      intent_state=intent_executor.ABANDONED_LOCAL)
    orchestrator.state.receipt(intent["intent_id"], result, [])
    return result


@guard_execution
def execute(
    orchestrator: Any, run_id: str, approval_id: str, input_hash: str, *,
    argv_transport: Any = None, icode_binary: str | None = None,
) -> dict[str, Any]:
    """Re-plan and check authorization before touching any workspace files."""
    planned = plan(orchestrator, run_id)
    if planned.get("reason_code") != "WRITEBACK_READY":
        return planned
    if planned["input_hash"] != input_hash:
        return _failure("WRITEBACK_INPUT_MISMATCH", run_id)
    approval = orchestrator.approvals.get(approval_id)
    if not isinstance(approval, dict):
        return _failure("WRITEBACK_APPROVAL_REQUIRED", run_id)
    if (approval.get("run_id") != run_id or gate_of(approval.get("action")) != ACTION
            or approval.get("input_hash") != input_hash):
        return _failure("WRITEBACK_APPROVAL_MISMATCH", run_id)
    if approval.get("effective_decision") == "REJECT":
        return _failure("WRITEBACK_REJECTED", run_id)
    if approval.get("effective_decision") != "APPROVE":
        return _failure("WRITEBACK_APPROVAL_REQUIRED", run_id)

    # A closed attempt (proven local failure, or a refusal from the only push target)
    # does not hold the key forever: the retry claims the next key in the chain.
    key = orchestrator.state.live_idempotency_key(_intent_key(run_id, input_hash))
    completed = orchestrator.state.result_by_idempotency_key(key)
    if completed is not None:
        response = completed["receipt"]["response"]
        return {**response, **({"reason_code": "WRITEBACK_REPLAY"} if response.get("ok") else {})}
    loaded = orchestrator._runtime_profile(run_id)
    if not loaded.get("ok"):
        return loaded
    transport = argv_transport if argv_transport is not None else ProcessTransport()
    binary = icode_binary or os.environ.get("ICODE_CLI_PATH") or "icode"
    existing = orchestrator.state.intent_by_idempotency_key(key)
    if existing is not None:
        return _observe(orchestrator, existing, transport, binary)

    proposal = planned["proposal"]
    events = orchestrator.state.events(run_id)
    pin = workspace_knowledge.pinned(events)
    intake = events[0]["payload"]
    binding = intake.get("collaboration_binding") or {}
    card_id = binding.get("card_id") or intake.get("requirement_id")
    if not isinstance(card_id, str) or not card_id.strip() or len(card_id.splitlines()) != 1:
        return _failure("WRITEBACK_CARD_MISSING", run_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        return _failure("WRITEBACK_RUN_ID_INVALID", run_id)
    branch = f"tom-autodev/{run_id[:12]}"
    payload = {
        "run_id": run_id, "input_hash": input_hash, "approval_id": approval_id,
        "repository": proposal["repository"], "path": str(Path(pin["path"]).expanduser().resolve()),
        "commit": proposal["commit"], "branch": branch, "target_branch": "master",
        "change_id": "I" + input_hash[:40], "card_id": card_id,
    }
    # The worktree is based on the pin and push_cr pushes every commit above
    # origin/master: an unreviewed local pin would be pushed for review with it.
    if not _published(Path(payload["path"]), proposal["commit"]):
        return _failure("WRITEBACK_PIN_UNPUBLISHED", run_id)
    claim = orchestrator.state.claim_intent(run_id, _OPERATION, key, payload)
    if claim["status"] == "CONFLICT":
        return _failure("WRITEBACK_INTENT_CONFLICT", run_id)
    intent = claim["intent"]
    if claim["status"] == "EXISTING":
        completed = orchestrator.state.result_by_idempotency_key(key)
        if completed is not None:
            response = completed["receipt"]["response"]
            return {**response, **({"reason_code": "WRITEBACK_REPLAY"} if response.get("ok") else {})}
        return _observe(orchestrator, intent, transport, binary)

    repo = Path(payload["path"])
    with tempfile.TemporaryDirectory(prefix="tom-autodev-knowledge-") as temporary:
        worktree = Path(temporary) / "workspace"
        added = False
        original_gitfile = None
        try:
            try:
                branches = _git(repo, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}")
                if f"refs/heads/{branch}" in branches.splitlines():
                    # Left by an earlier attempt that died before its cleanup. Only a
                    # branch no worktree has checked out is ours to drop.
                    listing = _git(repo, "worktree", "list", "--porcelain")
                    if f"branch refs/heads/{branch}" in listing.splitlines():
                        raise _LocalError("WRITEBACK_BRANCH_EXISTS")
                    _git(repo, "branch", "-D", branch)
                _git(repo, "worktree", "add", "-b", branch, str(worktree), proposal["commit"])
                added = True
                changed = _write_candidates(worktree, proposal, card_id)
                _git(worktree, "add", "--", *changed)
                staged = set(_git(worktree, "diff", "--cached", "--name-only", "-z").split("\0"))
                if any(item["target"] not in staged for item in proposal["candidates"]):
                    raise _LocalError("WRITEBACK_STAGE_MISMATCH")
                _git(worktree, "commit", "--no-gpg-sign", "--file=-",
                     input_text=_message(card_id, proposal, payload["change_id"]))
                revision = _git(worktree, "rev-parse", "HEAD").strip()
                orchestrator.state.record_external_observation(intent["intent_id"], {
                    "operation": "workspace.writeback.commit", "commit_revision": revision,
                })
                # Recheck pinned execution/profile contracts immediately before dispatch.
                blocked = execution_guard(orchestrator.state, run_id)
                if blocked is not None:
                    return _local_failure(orchestrator, intent, blocked["reason_code"])
                current_profile = orchestrator._runtime_profile(run_id)
                if not current_profile.get("ok"):
                    return _local_failure(orchestrator, intent, current_profile["reason_code"])
                original_gitfile = (worktree / ".git").read_bytes()
                _prepare_cli_worktree(worktree)
                single_target = _single_push_target(worktree)
            except _LocalError as error:
                return _local_failure(orchestrator, intent, str(error))
            except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
                return _local_failure(orchestrator, intent, "WRITEBACK_LOCAL_FAILED")
            try:
                orchestrator.state.record_external_observation(intent["intent_id"], {
                    "operation": "workspace.writeback.dispatch",
                })
                completed = orchestrator.state.result_by_idempotency_key(key)
                if completed is not None:
                    response = completed["receipt"]["response"]
                    return {**response, **({"reason_code": "WRITEBACK_REPLAY"} if response.get("ok") else {})}
                sticky = intent_executor.sticky_ambiguous(orchestrator.state, intent)
                if sticky is not None:
                    return _answer(orchestrator, intent, sticky)
                pushed = _run(
                    transport, [binary, "git", "push_cr", "--repo-path", str(worktree), "--branch", "master"],
                    cwd=worktree, timeout=_PUSH_TIMEOUT,
                )
            except Exception:
                return _answer(orchestrator, intent, intent_executor.Verdict.unknown())
            output = f"{pushed['stdout']}\n{pushed['stderr']}"
            if pushed["returncode"] != 0:
                if not _push_refused(output, single_target):
                    return _answer(orchestrator, intent, intent_executor.Verdict.unknown(
                        _failure("WRITEBACK_RESULT_UNKNOWN", run_id, **_bounded_cli_output(pushed))))
                # The only push target refused it: proven not landed, so retryable.
                result = _failure("WRITEBACK_PUSH_REJECTED", run_id, intent_id=intent["intent_id"],
                                  retry_allowed=True, intent_state=intent_executor.CONFIRMED_ABSENT,
                                  **_bounded_cli_output(pushed))
                orchestrator.state.receipt(intent["intent_id"], result, [])
                return result
            parsed = _pushed_url(output)
            if parsed is None:
                return _answer(orchestrator, intent, intent_executor.Verdict.unknown(
                    _failure("WRITEBACK_RESULT_UNKNOWN", run_id, **_bounded_cli_output(pushed))))
            number, url = parsed
            return _receipt(orchestrator, intent, number, url, revision)
        finally:
            if original_gitfile is not None:
                marker = worktree / ".git"
                marker.unlink(missing_ok=True)
                marker.write_bytes(original_gitfile)
            if added:
                _git(repo, "worktree", "remove", "--force", str(worktree))
                # The commit is recorded in the intent's observations and, once pushed,
                # lives on the review server; the local branch would only block a retry.
                try:
                    _git(repo, "branch", "-D", branch)
                except (OSError, subprocess.SubprocessError):
                    pass  # a leftover unused branch is dropped by the next attempt
