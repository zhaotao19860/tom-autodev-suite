"""iCode's AI review (小码哥), as a ledger-backed boundary.

The platform hands the conversation id back exactly once, at trigger time, and
`get_ai_review` accepts nothing else -- there is no way to ask "which conversation
belongs to this change". So a trigger whose id is not persisted is money spent and
findings lost, which is precisely what happened before this module existed. Every
trigger therefore claims an intent first and writes the id into a receipt, and the
replay path returns the recorded id rather than triggering again.

Polling is a read and stays outside the ledger.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from state_store import StateStore


OPERATION = "icode.ai_review"
_REQUIRED_COMMANDS = ("api",)


class IcodeAiReview:
    def __init__(
        self,
        *,
        state_store: StateStore,
        run_id: str,
        argv_transport: Any,
        working_directory: Path | str,
        artifact_store: Any = None,
        binary_candidates: list[str] | None = None,
        executable_resolver: Callable[[str], str | None] | None = None,
        sleeper: Callable[[float], None] | None = None,
        poll_interval: float = 30,
        max_polls: int = 30,
    ):
        self.state = state_store
        self.run_id = run_id
        self.transport = argv_transport
        self.cwd = Path(working_directory).expanduser()
        self.artifacts = artifact_store
        self.binary_candidates = list(binary_candidates or _default_candidates())
        self.executable_resolver = executable_resolver or _resolve_executable
        self.sleeper = sleeper or _sleep
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def start(self, change_number: Any, revision: Any) -> dict[str, Any]:
        """Trigger one review, or replay the conversation id already recorded."""
        if not _nonempty(revision) or not _positive_int(change_number):
            return _failure("AI_REVIEW_TARGET_INVALID")
        change_number = int(change_number)
        key = f"{OPERATION}:{self.run_id}:{change_number}:{revision}"
        payload = {
            "run_id": self.run_id,
            "change_number": change_number,
            "revision": str(revision),
        }
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return dict(completed["receipt"]["response"])
        claim = self.state.claim_intent(self.run_id, OPERATION, key, payload)
        if claim["status"] == "CONFLICT":
            return _failure("AI_REVIEW_CONFLICT")
        if claim["status"] == "EXISTING":
            # A previous trigger claimed this change and left no receipt. The platform
            # offers no way to recover the id from the change number, so re-triggering
            # would spend another review and still lose the first. Surface it.
            return _failure(
                "AI_REVIEW_CONVERSATION_LOST", intent_id=claim["intent"]["intent_id"]
            )
        intent = claim["intent"]
        cli = self._cli()
        if cli.get("reason_code") != "OK":
            return cli
        result = _run(
            self.transport,
            [cli["cli"], "api", "start_ai_review", "-n", str(change_number), "-o", "json"],
            cwd=self.cwd,
            timeout=120,
        )
        if result["returncode"] != 0:
            return _failure("AI_REVIEW_TRIGGER_FAILED", intent_id=intent["intent_id"])
        body = _decode(result["stdout"])
        if body is None:
            return _failure("AI_REVIEW_RESPONSE_INVALID", intent_id=intent["intent_id"])
        if str(body.get("status") or "") != "OK":
            return _failure(
                "AI_REVIEW_REJECTED",
                intent_id=intent["intent_id"],
                detail=str(body.get("message") or "")[:200],
            )
        conversation_id = _conversation_id(body)
        if not conversation_id:
            return _failure("AI_REVIEW_RESPONSE_INVALID", intent_id=intent["intent_id"])
        response = {
            "ok": True,
            "reason_code": "OK",
            "run_id": self.run_id,
            "change_number": change_number,
            "revision": str(revision),
            "conversation_id": conversation_id,
        }
        self.state.receipt(
            intent["intent_id"], response, [f"icode-ai-review-{conversation_id}"]
        )
        return response

    def poll(self, conversation_id: Any) -> dict[str, Any]:
        """Read one review's findings, waiting while the platform still owes them."""
        if not _nonempty(conversation_id):
            return _failure("AI_REVIEW_CONVERSATION_REQUIRED")
        cli = self._cli()
        if cli.get("reason_code") != "OK":
            return cli
        for attempt in range(self.max_polls):
            result = _run(
                self.transport,
                [cli["cli"], "api", "get_ai_review", "-i", str(conversation_id), "-o", "json"],
                cwd=self.cwd,
                timeout=120,
            )
            if result["returncode"] != 0:
                return _failure("AI_REVIEW_QUERY_FAILED", status="TRANSIENT")
            body = _decode(result["stdout"])
            if body is None:
                return _failure("AI_REVIEW_RESPONSE_INVALID")
            if str(body.get("status") or "") != "OK":
                return _failure(
                    "AI_REVIEW_QUERY_REJECTED", detail=str(body.get("message") or "")[:200]
                )
            data = body.get("data")
            if not isinstance(data, dict):
                # Same flattening as the trigger: when the CLI drops the envelope the
                # findings sit beside `status`/`message` rather than under `data`.
                data = {
                    key: value for key, value in body.items()
                    if key not in {"status", "message"}
                }
            if _settled(data):
                findings = _findings(data)
                response = {
                    "ok": True,
                    "reason_code": "OK",
                    "status": "SETTLED",
                    "conversation_id": str(conversation_id),
                    "findings": findings,
                    "review_report": _review_report(findings),
                    # The result schema is not pinned by any contract this repository
                    # owns, so the decoded payload travels with the parse rather than
                    # being thrown away when a field moves.
                    "raw": data,
                }
                if self.artifacts is not None:
                    archive = json.dumps(
                        {
                            "conversation_id": str(conversation_id),
                            "raw": data,
                            "findings": findings,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    response["artifact"] = self.artifacts.put(
                        self.run_id,
                        "ai-review",
                        archive,
                        {
                            "conversation_id": str(conversation_id),
                            "status": "SETTLED",
                            "source": "icode-cli.get_ai_review",
                        },
                    )
                return response
            if attempt + 1 < self.max_polls:
                self.sleeper(self.poll_interval)
        return _failure("AI_REVIEW_PENDING", status="PENDING", conversation_id=str(conversation_id))

    def _cli(self) -> dict[str, Any]:
        found = False
        for candidate in self.binary_candidates:
            if not candidate:
                continue
            resolved = self.executable_resolver(os.path.expanduser(candidate))
            if not resolved:
                continue
            found = True
            return {"ok": True, "reason_code": "OK", "cli": resolved}
        return _failure("ICODE_CLI_NOT_FOUND" if not found else "ICODE_SUBCOMMAND_MISSING")


def _conversation_id(body: dict[str, Any]) -> str:
    """The id, whether the CLI unwrapped the envelope's `data` or passed it through.

    The API document nests it under `data`; the CLI in use flattens it to the top
    level. Reading only one of the two is what threw away a paid-for trigger.
    """
    for holder in (body, body.get("data")):
        if isinstance(holder, dict):
            value = holder.get("conversationId") or holder.get("conversation_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _settled(data: Any) -> bool:
    """Whether the platform has stopped owing findings for this conversation."""
    if not isinstance(data, dict):
        return False
    state = str(data.get("crStatus") or data.get("status") or data.get("state") or "").upper()
    if state in {"EXECUTING", "RUNNING", "PENDING", "PROCESSING", "IN_PROGRESS", "QUEUED", ""}:
        return bool(_findings(data))
    return True


def _findings(data: Any) -> list[dict[str, Any]]:
    """The findings, from whichever key this deployment puts them under."""
    if not isinstance(data, dict):
        return []
    for key in ("results", "comments", "findings", "issues", "reviews", "suggestions"):
        value = data.get(key)
        if isinstance(value, list):
            if any(isinstance(item, dict) and "contentType" in item for item in value):
                return _transcript_messages(value)
            return [item for item in value if isinstance(item, dict)]
    return []


def _transcript_messages(items: list[Any]) -> list[dict[str, Any]]:
    """Reduce icode-cli's message transcript to review-bearing messages.

    The deployed API puts the whole 小码哥 conversation in ``results``.  Returning
    every tool call as a finding made callers treat the model's private work log as
    defects.  Keep human-readable review messages and scores, while retaining the
    original content only when it is already a compact scalar.
    """
    ignored = {"THINKING_TOOL_CALL", "INTERLINEAR_COMMENT", "CODE_CHANGES"}
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("contentType") or "")
        if content_type in ignored:
            continue
        processed = item.get("processedContent")
        text = processed.get("text") if isinstance(processed, dict) else None
        if not isinstance(text, str) or not text.strip():
            original = item.get("originalContent")
            text = original if isinstance(original, str) else ""
        if not text.strip():
            continue
        messages.append({
            "content_type": content_type or "OTHER",
            "text": text,
        })
    return messages


def _review_report(findings: list[dict[str, Any]]) -> str:
    """Return the latest prose report, if the platform supplied one."""
    for item in reversed(findings):
        if item.get("content_type") == "OTHER" and item.get("text"):
            return item["text"]
    return ""


def _default_candidates() -> list[str]:
    return [
        os.environ.get("ICODE_CLI_PATH", ""),
        "icode-cli",
        str(Path.home() / ".icode" / "bin" / "icode-cli"),
        "icode",
        str(Path.home() / ".icode" / "bin" / "icode"),
    ]


def _resolve_executable(value: str) -> str | None:
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or "/" in value:
        return str(expanded) if expanded.is_file() and os.access(expanded, os.X_OK) else None
    return shutil.which(value)


def _run(transport: Any, argv: list[str], *, cwd: Path, timeout: int) -> dict[str, Any]:
    result = transport.run(argv, cwd=cwd, timeout=timeout)
    if isinstance(result, dict):
        code = result.get("returncode", result.get("exit_code", 0))
        stdout, stderr = result.get("stdout", ""), result.get("stderr", "")
    else:
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    return {"returncode": int(code), "stdout": str(stdout or ""), "stderr": str(stderr or "")}


def _decode(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(text[start:])
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except Exception:
        return False


def _failure(reason_code: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **fields}
