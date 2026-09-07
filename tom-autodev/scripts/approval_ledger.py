from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from approval_contract import STRICT_APPROVAL_TIMEOUT_SECONDS
from persistence_policy import ensure_persistable

APPROVAL_CHANNELS = frozenset({"comate", "infoflow"})
APPROVAL_DECISIONS = frozenset({"APPROVE", "REJECT"})


class ApprovalLedger:
    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path); self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
              approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT 'legacy', action TEXT NOT NULL,
              input_hash TEXT NOT NULL, channels_json TEXT NOT NULL, member_policy_json TEXT NOT NULL DEFAULT '{}',
              delivery_receipts_json TEXT NOT NULL DEFAULT '[]', effective_decision TEXT, effective_channel TEXT,
              deadline_at TEXT, timeout_at TEXT, heartbeat_at TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
            CREATE TABLE IF NOT EXISTS approval_responses (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, response_id TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL,
              decision TEXT NOT NULL, input_hash TEXT NOT NULL, channel TEXT NOT NULL, responder TEXT,
              effective INTEGER NOT NULL, conflict INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1,
              rejected_reason TEXT, late INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS approval_responses_approval_sequence ON approval_responses(approval_id, sequence);
            """)
            _migrate(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Commits like `with sqlite3.connect(...)` and also closes, which that does not.
        connection = sqlite3.connect(self.database_path); connection.row_factory = sqlite3.Row
        try:
            with connection: yield connection
        finally:
            connection.close()

    def request(self, action: str, input_hash: str, channels: list[str], *, run_id: str | None = None, member_policy: dict[str, list[str]] | None = None, deadline_at: str | None = None) -> dict[str, Any]:
        if set(channels) != APPROVAL_CHANNELS or len(channels) != 2: raise ValueError("APPROVAL_CHANNEL_NOT_CONFIGURED")
        strict = run_id is not None
        run = run_id or "legacy"
        if strict and (not isinstance(run_id, str) or not run_id): raise ValueError("APPROVAL_RUN_INVALID")
        policy = _policy(member_policy, strict)
        deadline = _parse_deadline(deadline_at) if deadline_at is not None else (
            datetime.now(timezone.utc) + timedelta(seconds=STRICT_APPROVAL_TIMEOUT_SECONDS)
            if strict else None
        )
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM approvals WHERE run_id=? AND action=? AND input_hash=?", (run, action, input_hash)).fetchone()
            if row is not None: return _row(row)
            approval_id = uuid.uuid4().hex
            c.execute("INSERT INTO approvals(approval_id,run_id,action,input_hash,channels_json,member_policy_json,deadline_at,created_at) VALUES (?,?,?,?,?,?,?,?)", (approval_id,run,action,input_hash,json.dumps(channels),json.dumps(policy,sort_keys=True),deadline.isoformat() if deadline else None,_now()))
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return _row(row)

    def resolve(self, approval_id: str, decision: str, input_hash: str, channel: str, *, run_id: str | None = None, responder: str | None = None, now: datetime | None = None, state_store: Any | None = None) -> dict[str, Any]:
        result = self.receive(approval_id, decision, input_hash, channel, responder, run_id=run_id, now=now, state_store=state_store)
        if result.get("reason_code") not in {None, "OK"}: raise ValueError(result["reason_code"])
        return result

    def receive(self, approval_id: str, decision: str, input_hash: str, channel: str, responder: str | None, *, run_id: str | None = None, now: datetime | None = None, state_store: Any | None = None) -> dict[str, Any]:
        observed = now or datetime.now(timezone.utc)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE"); row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None: raise ValueError("APPROVAL_NOT_FOUND")
            reason = _reject(row, decision, input_hash, channel, responder, run_id)
            deadline = _parse_deadline(row["deadline_at"]) if row["deadline_at"] else None
            if reason is None and row["effective_decision"] is None and deadline is not None and observed >= deadline:
                reason = "APPROVAL_STATE_STORE_REQUIRED" if row["run_id"] != "legacy" and state_store is None else "APPROVAL_TIMEOUT"
            if reason is not None:
                if reason == "APPROVAL_TIMEOUT" and row["effective_decision"] is None:
                    c.execute("UPDATE approvals SET effective_decision='TIMEOUT',effective_channel='timeout',timeout_at=?,resolved_at=? WHERE approval_id=?", (observed.isoformat(), observed.isoformat(), approval_id))
                    row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
                _record(c, approval_id, decision, input_hash, channel, responder, False, False, False, reason, bool(row["effective_decision"]) or reason == "APPROVAL_TIMEOUT", observed)
                result = {**_row(row), "reason_code": reason, "conflict": False}
                if reason == "APPROVAL_TIMEOUT":
                    result["handoff"] = {"handoff_id": f"approval-timeout-{approval_id}", "status": "PENDING"}
                    if state_store is not None:
                        state_store.record_handoff(row["run_id"], result["handoff"]["handoff_id"], {"approval_id": approval_id, "input_hash": input_hash, "reason_code": reason})
                return result
            if row["effective_decision"]:
                conflict = row["effective_decision"] != decision or row["effective_channel"] != channel
                _record(c, approval_id, decision, input_hash, channel, responder, False, conflict, True, None, True, observed)
                return {**_row(row), "reason_code": "OK", "conflict": conflict}
            c.execute("UPDATE approvals SET effective_decision=?,effective_channel=?,resolved_at=? WHERE approval_id=?", (decision,channel,observed.isoformat(),approval_id))
            _record(c, approval_id, decision, input_hash, channel, responder, True, False, True, None, False, observed)
        return {"approval_id": approval_id, "run_id": row["run_id"], "input_hash": input_hash, "effective_decision": decision, "effective_channel": channel, "reason_code": "OK", "conflict": False}

    def timeout(self, approval_id: str, input_hash: str, *, run_id: str | None = None, state_store: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
        observed = now or datetime.now(timezone.utc)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE"); row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None: raise ValueError("APPROVAL_NOT_FOUND")
            if row["input_hash"] != input_hash: raise ValueError("APPROVAL_INPUT_MISMATCH")
            if row["run_id"] != "legacy" and (not isinstance(run_id, str) or row["run_id"] != run_id): return {**_row(row), "reason_code": "APPROVAL_RUN_MISMATCH"}
            if row["run_id"] != "legacy" and state_store is None: return {**_row(row), "reason_code": "APPROVAL_STATE_STORE_REQUIRED"}
            if row["effective_decision"] is not None: return {**_row(row), "reason_code": "APPROVAL_ALREADY_RESOLVED"}
            deadline = _parse_deadline(row["deadline_at"]) if row["deadline_at"] else None
            if deadline is not None and observed < deadline: return {**_row(row), "reason_code": "APPROVAL_TIMEOUT_EARLY"}
            c.execute("UPDATE approvals SET effective_decision='TIMEOUT',effective_channel='timeout',timeout_at=?,resolved_at=? WHERE approval_id=?", (observed.isoformat(),observed.isoformat(),approval_id))
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        handoff = {"handoff_id": f"approval-timeout-{approval_id}", "status": "PENDING"}
        if state_store is not None:
            state_store.record_handoff(row["run_id"], handoff["handoff_id"], {"approval_id": approval_id, "input_hash": input_hash, "reason_code": "APPROVAL_TIMEOUT"})
        return {**_row(row), "reason_code": "APPROVAL_TIMEOUT", "handoff": handoff}

    def record_delivery(self, approval_id: str, channel: str, receipt: dict[str, Any], *, payload_hash: str | None = None) -> dict[str, Any]:
        if channel not in APPROVAL_CHANNELS: raise ValueError("APPROVAL_CHANNEL_NOT_CONFIGURED")
        ensure_persistable(receipt)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE"); row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None: raise ValueError("APPROVAL_NOT_FOUND")
            entries = json.loads(row["delivery_receipts_json"]); entry = {"channel": channel, "payload_hash": payload_hash, "receipt": receipt}
            if entry not in entries:
                entries.append(entry); c.execute("UPDATE approvals SET delivery_receipts_json=? WHERE approval_id=?", (json.dumps(entries,sort_keys=True),approval_id)); row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return _row(row)

    def record_delivery_failure(self, approval_id: str, failure: dict[str, Any] | None) -> dict[str, Any]:
        """Mark -- or, with `failure=None`, clear -- an approval as undeliverable.

        A gate whose card reached nobody cannot be answered, yet the ledger row is
        unique per `(run_id, action, input_hash)`, so the failed attempt holds the only
        slot this question has and `reissue-approval`, which accepted a timeout and
        nothing else, could not free it. Recording the failure on the row is what lets
        that command tell "nobody was ever asked" from "somebody is still thinking".

        Clearing on a later success is part of the same contract: a retry that gets
        through must leave no trace of the attempt that did not, or the row keeps
        claiming undeliverable while a live card sits in front of a reviewer.
        """
        if failure is not None:
            ensure_persistable(failure)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None: raise ValueError("APPROVAL_NOT_FOUND")
            if row["effective_decision"] is not None: return {**_row(row), "reason_code": "APPROVAL_ALREADY_RESOLVED"}
            c.execute(
                "UPDATE approvals SET delivery_failed_at=?,delivery_failure_json=? WHERE approval_id=?",
                (_now() if failure is not None else None,
                 json.dumps(failure, ensure_ascii=False, sort_keys=True) if failure is not None else None,
                 approval_id),
            )
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return _row(row)

    def record_heartbeat(self, approval_id: str, observed_at: str) -> dict[str, Any]:
        _parse_deadline(observed_at)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE approvals SET heartbeat_at=? WHERE approval_id=?", (observed_at,approval_id)); row=c.execute("SELECT * FROM approvals WHERE approval_id=?",(approval_id,)).fetchone()
        if row is None: raise ValueError("APPROVAL_NOT_FOUND")
        return _row(row)

    def reject_envelope(
        self,
        approval_id: str,
        input_hash: str,
        channel: str,
        *,
        run_id: str,
        responder: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed = now or datetime.now(timezone.utc)
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("APPROVAL_NOT_FOUND")
            if row["run_id"] != run_id or row["run_id"] == "legacy":
                return {**_row(row), "reason_code": "APPROVAL_RUN_MISMATCH"}
            _record(
                c, approval_id, "", input_hash, channel, responder,
                False, False, False, "APPROVAL_ENVELOPE_INVALID", False, observed,
            )
        return {**_row(row), "reason_code": "APPROVAL_ENVELOPE_INVALID", "conflict": False}

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as c: row=c.execute("SELECT * FROM approvals WHERE approval_id=?",(approval_id,)).fetchone()
        return _row(row) if row else None
    def pending(self) -> list[dict[str, Any]]:
        """Unresolved strict approvals, oldest first, for the reply watcher.

        An undeliverable approval is not among them: no channel is showing that card,
        so no reply can arrive on it, and polling one only teaches the watcher to
        report silence as though somebody were still deciding.
        """
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM approvals WHERE effective_decision IS NULL AND run_id != 'legacy'"
                " AND delivery_failed_at IS NULL ORDER BY created_at, approval_id"
            ).fetchall()
        return [_row(row) for row in rows]

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM approvals WHERE run_id = ? ORDER BY created_at, approval_id", (run_id,)
            ).fetchall()
        return [_row(row) for row in rows]
    def responses(self, approval_id: str) -> list[dict[str, Any]]:
        with self._connect() as c: rows=c.execute("SELECT * FROM approval_responses WHERE approval_id=? ORDER BY sequence",(approval_id,)).fetchall()
        return [{"response_id":x["response_id"],"approval_id":x["approval_id"],"decision":x["decision"],"input_hash":x["input_hash"],"channel":x["channel"],"responder":x["responder"],"effective":bool(x["effective"]),"conflict":bool(x["conflict"]),"valid":bool(x["valid"]),"rejected_reason":x["rejected_reason"],"late":bool(x["late"]),"received_at":x["received_at"]} for x in rows]

def _migrate(c: sqlite3.Connection) -> None:
    for table, fields in {"approvals":{"run_id":"TEXT NOT NULL DEFAULT 'legacy'","member_policy_json":"TEXT NOT NULL DEFAULT '{}'","delivery_receipts_json":"TEXT NOT NULL DEFAULT '[]'","deadline_at":"TEXT","timeout_at":"TEXT","heartbeat_at":"TEXT","delivery_failed_at":"TEXT","delivery_failure_json":"TEXT"},"approval_responses":{"responder":"TEXT","valid":"INTEGER NOT NULL DEFAULT 1","rejected_reason":"TEXT","late":"INTEGER NOT NULL DEFAULT 0"}}.items():
        existing={r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for field,definition in fields.items():
            if field not in existing: c.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
    c.execute("DROP INDEX IF EXISTS approvals_action_hash")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS approvals_run_action_hash ON approvals(run_id,action,input_hash)")
def _policy(value: dict[str,list[str]]|None, strict: bool) -> dict[str,list[str]]:
    if value is None:
        if strict: raise ValueError("APPROVAL_MEMBER_POLICY_REQUIRED")
        return {}
    if set(value) != APPROVAL_CHANNELS or any(not isinstance(value[k],list) or not value[k] for k in APPROVAL_CHANNELS): raise ValueError("APPROVAL_MEMBER_POLICY_INVALID")
    result={k:sorted(set(value[k])) for k in APPROVAL_CHANNELS}
    if not all(all(isinstance(x,str) and "@" in x for x in values) for values in result.values()): raise ValueError("APPROVAL_MEMBER_POLICY_INVALID")
    ensure_persistable(result); return result
def _parse_deadline(value: str) -> datetime:
    try: parsed=datetime.fromisoformat(value)
    except (TypeError,ValueError): raise ValueError("APPROVAL_DEADLINE_INVALID") from None
    if parsed.tzinfo is None: raise ValueError("APPROVAL_DEADLINE_INVALID")
    return parsed.astimezone(timezone.utc)
def _reject(row:sqlite3.Row,decision:str,input_hash:str,channel:str,responder:str|None,run_id:str|None)->str|None:
    if row["run_id"] != "legacy" and (not isinstance(run_id, str) or row["run_id"] != run_id):return "APPROVAL_RUN_MISMATCH"
    if row["run_id"] == "legacy" and run_id is not None and row["run_id"] != run_id:return "APPROVAL_RUN_MISMATCH"
    if row["input_hash"] != input_hash:return "APPROVAL_INPUT_MISMATCH"
    if channel not in APPROVAL_CHANNELS:return "APPROVAL_CHANNEL_NOT_CONFIGURED"
    if decision not in APPROVAL_DECISIONS:return "APPROVAL_DECISION_INVALID"
    policy=json.loads(row["member_policy_json"])
    if policy.get(channel) and (not isinstance(responder,str) or responder not in policy.get(channel,[])):return "APPROVAL_RESPONDER_UNAUTHORIZED"
    if row["run_id"] != "legacy":
        deliveries = json.loads(row["delivery_receipts_json"])
        channels = {entry.get("channel") for entry in deliveries if entry.get("payload_hash") == row["input_hash"]}
        if channels != set(APPROVAL_CHANNELS): return "APPROVAL_DELIVERY_INCOMPLETE"
    return None
def _record(c:sqlite3.Connection,approval_id:str,decision:str,input_hash:str,channel:str,responder:str|None,effective:bool,conflict:bool,valid:bool,rejected:str|None,late:bool,now:datetime)->None:
    c.execute("INSERT INTO approval_responses(response_id,approval_id,decision,input_hash,channel,responder,effective,conflict,valid,rejected_reason,late,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,approval_id,decision,input_hash,channel,responder,int(effective),int(conflict),int(valid),rejected,int(late),now.isoformat()))
def _row(row:sqlite3.Row)->dict[str,Any]:
    decision=row["effective_decision"]
    undelivered=row["delivery_failed_at"] if "delivery_failed_at" in row.keys() else None
    return {"approval_id":row["approval_id"],"run_id":row["run_id"],"action":row["action"],"input_hash":row["input_hash"],"channels":json.loads(row["channels_json"]),"member_policy":json.loads(row["member_policy_json"]),"delivery_receipts":json.loads(row["delivery_receipts_json"]),"deadline_at":row["deadline_at"],"heartbeat_at":row["heartbeat_at"],"status":"TIMEOUT" if decision=="TIMEOUT" else ("RESOLVED" if decision else ("DELIVERY_FAILED" if undelivered else "PENDING")),"effective_decision":decision,"effective_channel":row["effective_channel"],
    # Why the card never landed, for `reissue-approval` and for the operator reading
    # the ledger. Absent once a retry delivers, so it never outlives the attempt.
    "delivery_failed_at":undelivered,"delivery_failure":json.loads(row["delivery_failure_json"]) if undelivered and row["delivery_failure_json"] else None,
    # When the decision landed. A reader has to be able to tell a decision that
    # answers the current phase from one left over from an earlier attempt at it.
    "resolved_at":row["resolved_at"]}
def _now()->str:return datetime.now(timezone.utc).isoformat()
