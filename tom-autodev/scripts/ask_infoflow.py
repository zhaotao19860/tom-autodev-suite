"""Ask a run's owner a non-gate question in 如流 and read the tapped answer.

A run does not only stop at approval gates. It also reaches points where the next
step is a judgement call — which repair to take first, whether to continue into the
next phase — and those used to be asked only in the IDE, where nobody sees them until
they come back to look. This sends the question as a button card to the run's
collaboration group, so the phone shows that a decision is waiting and one tap
answers it.

It is deliberately weaker than an approval gate and must never be used as one: there
is no ledger record, no deadline binding and no member policy behind it. A gate stays
a gate (`approval_watch`), and the only thing trusted here is that the answer was
tapped in the run's own group after the question was asked.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clients.infoflow_bot_client import InfoflowBotClient
from clients.infoflow_reply_client import InfoflowReplyJournal
from collaboration import group_id_for_run
from state_store import StateStore


_HOME = Path.home() / ".tom-autodev"
_ANSWER = re.compile(r"^CHOICE ([0-9a-f]{16}) ([a-z0-9_-]{1,32})$")


def _record_path(decision_id: str) -> Path:
    return _HOME / "decisions" / f"{decision_id}.json"


def _group(run_id: str) -> str:
    group_id = group_id_for_run(StateStore(_HOME / "state.sqlite"), run_id)
    if not isinstance(group_id, str) or not group_id:
        # Without a group there is nowhere to ask that the answer could be scoped to.
        raise SystemExit("NO_COLLABORATION_GROUP")
    return group_id


def ask(args: argparse.Namespace) -> dict[str, Any]:
    options = []
    for raw in args.option:
        key, _, text = raw.partition("=")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", key) or not text.strip():
            raise SystemExit(f"OPTION_INVALID:{raw}")
        options.append({"key": key, "text": text.strip()})
    if not 2 <= len(options) <= 6:
        raise SystemExit("OPTION_COUNT_INVALID")
    group_id = _group(args.run_id)
    decision_id = secrets.token_hex(8)
    asked_at = datetime.now(timezone.utc).isoformat()
    client = InfoflowBotClient()
    receipt = client.send_choice_card(
        decision_id=decision_id,
        target_type="group",
        target_id=group_id,
        title=args.title,
        question=args.question,
        lines=list(args.line),
        options=options,
    )
    record = {
        "decision_id": decision_id,
        "run_id": args.run_id,
        "group_id": group_id,
        "asked_at": asked_at,
        "title": args.title,
        "options": options,
        "card_id": receipt["card_id"],
    }
    path = _record_path(decision_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def _answer(record: dict[str, Any], journal: InfoflowReplyJournal) -> dict[str, Any] | None:
    keys = {option["key"] for option in record["options"]}
    for message in journal.messages():
        # An answer only counts from the group the question was asked in, and only
        # after it was asked: an older line with the same words is not this answer.
        if message.get("chat_type") != "group" or message.get("group_id") != record["group_id"]:
            continue
        if message["received_at"] <= record["asked_at"]:
            continue
        found = _ANSWER.match(message["text"].strip())
        if found is None or found.group(1) != record["decision_id"] or found.group(2) not in keys:
            continue
        return {
            "decision_id": record["decision_id"],
            "option": found.group(2),
            "responder": message["sender"],
            "received_at": message["received_at"],
        }
    return None


def wait(args: argparse.Namespace) -> dict[str, Any]:
    path = _record_path(args.decision_id)
    if not path.exists():
        raise SystemExit("DECISION_UNKNOWN")
    record = json.loads(path.read_text(encoding="utf-8"))
    journal = InfoflowReplyJournal()
    deadline = time.monotonic() + args.timeout
    while True:
        answer = _answer(record, journal)
        if answer is not None:
            return {"status": "ANSWERED", **answer}
        if time.monotonic() >= deadline:
            # A timeout is not a decision: the caller falls back to asking in the IDE.
            return {"status": "TIMEOUT", "decision_id": record["decision_id"]}
        time.sleep(args.poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ask_infoflow")
    commands = parser.add_subparsers(dest="command", required=True)
    asking = commands.add_parser("ask")
    asking.add_argument("run_id")
    asking.add_argument("--title", required=True)
    asking.add_argument("--question", default="")
    asking.add_argument("--line", action="append", default=[])
    asking.add_argument("--option", action="append", required=True)
    asking.set_defaults(handler=ask)
    waiting = commands.add_parser("wait")
    waiting.add_argument("run_id")
    waiting.add_argument("decision_id")
    waiting.add_argument("--timeout", type=float, default=900.0)
    waiting.add_argument("--poll", type=float, default=5.0)
    waiting.set_defaults(handler=wait)
    args = parser.parse_args(argv)
    print(json.dumps(args.handler(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
