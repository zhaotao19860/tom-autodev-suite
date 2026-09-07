from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DECISIONS = ("APPROVE", "REJECT")
_WORD = re.compile(r"[A-Za-z]+")
_JOURNAL_ENV = "TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL"


class InfoflowReplyJournal:
    """Append-only inbound message log written by the Infoflow WebSocket relay.

    The installed Infoflow scripts can only read a message whose id is already
    known, so a decision typed in 如流 cannot be discovered by polling. The relay
    (`infoflow-gateway/reply_relay.mjs`) holds the long-lived WebSocket instead and
    appends every inbound message here as one JSON object per line. Reading is
    deliberately dumb: this class decides nothing and trusts nothing, it only hands
    over the lines it can parse so the consumer can authorize them.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(
            path
            or os.environ.get(_JOURNAL_ENV)
            or Path.home() / ".tom-autodev" / "infoflow-replies.jsonl"
        ).expanduser()

    def messages(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        messages = [
            message
            for message in (_message(line) for line in raw.splitlines())
            if message is not None
        ]
        # A relay restart can re-deliver a message, and two relays can interleave
        # lines, so order by the observed time and keep the id as the tie-break.
        return sorted(messages, key=lambda item: (item["received_at"], item["message_id"]))


class InfoflowReplyConsumer:
    """Turn journalled 如流 messages into a bound approval reply, or nothing.

    A reply is only a decision when it names its approval: `approval_id` is unique
    per `(run_id, action, input_hash)` in the ledger, so quoting it binds the answer
    to the exact content hash that was sent for review. Anything else — an
    unauthorized sender, an ambiguous body, a message from before the request or
    after its deadline — is left in the journal untouched.

    Requests are delivered to the run's collaboration group, so a reply typed there
    counts too. `group_resolver` maps a run to that group, and a group reply is only
    read when it was typed in it: the same words in an unrelated group are not a
    decision about this run.
    """

    def __init__(self, journal: Any, *, clock: Any | None = None, group_resolver: Any | None = None):
        self.journal = journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.group_resolver = group_resolver

    def resolve(self, record: Any) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        required = ("request_id", "approval_id", "run_id", "input_hash", "created_at", "deadline_at")
        if any(not isinstance(record.get(field), str) or not record[field] for field in required):
            return None
        members = record.get("member_policy", {})
        members = members.get("infoflow") if isinstance(members, dict) else None
        if not isinstance(members, list) or not members:
            return None
        opened = _instant(record["created_at"])
        deadline = _instant(record["deadline_at"])
        if opened is None or deadline is None:
            return None
        group_id = self._group_id(record["run_id"])
        for message in self.journal.messages():
            received = _instant(message["received_at"])
            if received is None or received < opened or received > deadline:
                continue
            if not _in_scope(message, group_id):
                continue
            if not _binds(message["text"], record):
                continue
            responder = _responder(message["sender"], members)
            decision = _decision(message["text"])
            if responder is None or decision is None:
                continue
            return {
                "reply_id": f"infoflow-{message['message_id']}",
                "decision": decision,
                "responder": responder,
                "received_at": received.isoformat(),
            }
        return None

    def _group_id(self, run_id: str) -> str | None:
        if self.group_resolver is None:
            return None
        try:
            found = self.group_resolver(run_id)
        except Exception:  # noqa: BLE001 - a lookup failure must not authorize anything
            return None
        return found if isinstance(found, str) and found else None


def _in_scope(message: dict[str, Any], group_id: str | None) -> bool:
    """Where the reply was typed has to match where the request was sent.

    A private reply is always about its own approval. A group reply is read only when
    the run has a known group and the message came from it; without a resolved group
    there is nothing to compare against, so the group is not trusted.
    """
    if message.get("chat_type") != "group":
        return True
    return group_id is not None and message.get("group_id") == group_id


def _message(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    fields = {field: payload.get(field) for field in ("message_id", "sender", "text", "received_at")}
    if any(not isinstance(value, str) or not value for value in fields.values()):
        return None
    group_id = payload.get("group_id")
    return {
        **fields,
        "chat_type": payload.get("chat_type"),
        "group_id": group_id if isinstance(group_id, str) and group_id else None,
    }


def _binds(text: str, record: dict[str, Any]) -> bool:
    return record["approval_id"] in text or record["request_id"] in text


def _decision(text: str) -> str | None:
    words = {word.upper() for word in _WORD.findall(text)}
    found = [decision for decision in _DECISIONS if decision in words]
    return found[0] if len(found) == 1 else None


def _responder(sender: str, members: list[str]) -> str | None:
    """Map the sender's uuap back onto the member policy, never the other way."""
    candidates = [
        member
        for member in members
        if isinstance(member, str) and member.split("@", 1)[0] == sender
    ]
    return candidates[0] if len(candidates) == 1 else None


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
