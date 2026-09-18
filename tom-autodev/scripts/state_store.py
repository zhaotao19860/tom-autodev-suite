from __future__ import annotations

import base64
import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from persistence_policy import ensure_persistable, validate_evidence_refs


class StateStore:
    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Hand out a connection that is committed *and* closed on the way out.

        `with sqlite3.connect(...) as connection` only ends the transaction; the handle
        stays open until the garbage collector gets to it. Every method here opens one,
        so a run leaked a file descriptor per call and the test suite printed a wall of
        `ResourceWarning`s that hid real output. Closing at the single place they are
        created beats remembering it at each of the thirty call sites -- two of which had
        already grown their own `try/finally` for exactly this reason.
        """
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_sequence
                    ON events(run_id, sequence);
                CREATE TABLE IF NOT EXISTS idempotency_results (
                    idempotency_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_intents (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_intents_run_sequence
                    ON external_intents(run_id, sequence);
                CREATE TABLE IF NOT EXISTS receipts (
                    intent_id TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_results (
                    intent_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_results_run_operation
                    ON external_results(run_id, operation);
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    metadata_path TEXT NOT NULL,
                    metadata_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS locks (
                    lock_key TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS heartbeats (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    lock_key TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    kind TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS heartbeats_lock_sequence
                    ON heartbeats(lock_key, sequence);
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS handoffs_run_id
                    ON handoffs(run_id, handoff_id);
                CREATE TABLE IF NOT EXISTS optimization_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS optimization_proposals_run_id
                    ON optimization_proposals(run_id, created_at);
                CREATE TABLE IF NOT EXISTS producer_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS producer_jobs_run_id
                    ON producer_jobs(run_id, job_id);
                CREATE TABLE IF NOT EXISTS model_execution_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    phase TEXT,
                    input_hash TEXT,
                    output_hash TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS model_execution_receipts_run_id
                    ON model_execution_receipts(run_id, created_at);
                CREATE TABLE IF NOT EXISTS draft_cache (
                    cache_key TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            _optimization_migrate(connection)

    def transition(self, run_id: str, state: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "event_id": uuid.uuid4().hex,
            "run_id": run_id,
            "state": state,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(event_id, run_id, state, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["run_id"],
                    event["state"],
                    _encode(payload),
                    event["created_at"],
                ),
            )
        return event

    def latest_states(self) -> list[dict[str, Any]]:
        """The newest event of every run, so a watcher can notice a state change.

        There is no run table; the event log is the record, and a run's last event
        is its state.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, state, created_at
                FROM events
                WHERE sequence IN (SELECT MAX(sequence) FROM events GROUP BY run_id)
                ORDER BY created_at
                """
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "state": row["state"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, state, payload_json, created_at
                FROM events
                WHERE run_id = ?
                ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "state": row["state"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def save_idempotency_result(self, key: str, result: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(result)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT result_json FROM idempotency_results WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["result_json"] != encoded:
                    raise ValueError(f"idempotency result already exists for {key}")
                return json.loads(existing["result_json"])
            connection.execute(
                """
                INSERT INTO idempotency_results(idempotency_key, result_json, created_at)
                VALUES (?, ?, ?)
                """,
                (key, encoded, created_at),
            )
        return result

    def idempotency_result(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM idempotency_results WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return json.loads(row["result_json"]) if row is not None else None

    def commit_transition_result(
        self,
        run_id: str,
        expected_event_id: str,
        state: str,
        payload: dict[str, Any],
        result_key: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """CAS the current checkpoint and persist its definite result atomically."""
        payload_json = _encode(payload)
        requested_result_json = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT result_json FROM idempotency_results WHERE idempotency_key = ?",
                (result_key,),
            ).fetchone()
            if existing is not None:
                saved = json.loads(existing["result_json"])
                if any(saved.get(key) != value for key, value in result.items()):
                    return {"status": "RESULT_CONFLICT", "result": saved}
                return {"status": "REPLAY", "result": saved}
            current = connection.execute(
                "SELECT event_id FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if current is None or current["event_id"] != expected_event_id:
                return {"status": "SOURCE_EVENT_MISMATCH"}
            event_id = uuid.uuid4().hex
            created_at = _now()
            final_result = {
                **json.loads(requested_result_json),
                "run_id": run_id,
                "state": state,
                "event_id": event_id,
            }
            connection.execute(
                """
                INSERT INTO events(event_id, run_id, state, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, run_id, state, payload_json, created_at),
            )
            connection.execute(
                """
                INSERT INTO idempotency_results(idempotency_key, result_json, created_at)
                VALUES (?, ?, ?)
                """,
                (result_key, _encode(final_result), created_at),
            )
        return {"status": "COMMITTED", "result": final_result}

    def intent(
        self,
        run_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = _encode(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM external_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != run_id
                    or existing["operation"] != operation
                    or existing["payload_json"] != encoded
                ):
                    raise ValueError("INTENT_CONFLICT")
                return _intent_row(existing)
            intent_id = uuid.uuid4().hex
            created_at = _now()
            connection.execute(
                """
                INSERT INTO external_intents(
                    intent_id, run_id, operation, idempotency_key, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (intent_id, run_id, operation, idempotency_key, encoded, created_at),
            )
            row = connection.execute(
                "SELECT * FROM external_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return _intent_row(row)

    def claim_intent(
        self,
        run_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically claim responsibility for one external write.

        `intent()` remains compatible with older callers. New write paths need the
        created flag because only the transaction that inserted the intent may send.
        """
        encoded = _encode(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM external_intents WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != run_id or existing["operation"] != operation:
                    return {"status": "CONFLICT", "intent": _intent_row(existing)}
                if existing["payload_json"] != encoded:
                    return {"status": "CONFLICT", "intent": _intent_row(existing)}
                return {"status": "EXISTING", "intent": _intent_row(existing)}
            intent_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO external_intents(
                    intent_id, run_id, operation, idempotency_key, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (intent_id, run_id, operation, idempotency_key, encoded, _now()),
            )
            row = connection.execute(
                "SELECT * FROM external_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return {"status": "CLAIMED", "intent": _intent_row(row)}

    def intent_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM external_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _intent_row(row) if row is not None else None

    def live_idempotency_key(self, base_key: str, *, depth: int = 8) -> str:
        """The key a fresh attempt should claim, skipping abandoned predecessors.

        An abandonment says "we stopped waiting on that write", not "that write
        succeeded" and not "nothing was written". Its receipt is write-once, so an
        operation keyed only by its own content can never be attempted again: the
        replay finds the abandoned receipt and returns it forever, which is how a
        knowledge publish for change set T3 wedged a run that had already been fixed.
        Chaining the next attempt off the intent it supersedes keeps both accounts —
        the closed attempt keeps its intent and receipt — while making the retry
        itself idempotent.
        """
        key = base_key
        for _ in range(depth):
            completed = self.result_by_idempotency_key(key)
            response = completed["receipt"]["response"] if isinstance(completed, dict) else None
            if not _superseded_external_receipt(response):
                return key
            prior = self.intent_by_idempotency_key(key)
            if not isinstance(prior, dict):
                return key
            key = f"{base_key}:after:{prior['intent_id']}"
        return key

    def withdraw_intent(
        self,
        run_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Drop a claim for a write that provably never left this process.

        Uncertainty must never be resolved by guessing, so this is only for the
        narrow case where the client rejected the call before dispatching it: the
        alternative is a run stuck in `RECOVERY_REQUIRED` over an action nobody
        performed. A claim that already carries a receipt is left untouched.
        """
        encoded = _encode(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT intent.*, receipt.intent_id AS receipt_id
                FROM external_intents AS intent
                LEFT JOIN receipts AS receipt ON receipt.intent_id = intent.intent_id
                WHERE intent.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return {"status": "MISSING"}
            if (
                row["run_id"] != run_id
                or row["operation"] != operation
                or row["payload_json"] != encoded
                or row["receipt_id"] is not None
            ):
                return {"status": "CONFLICT", "intent": _intent_row(row)}
            connection.execute(
                "DELETE FROM external_intents WHERE intent_id = ?", (row["intent_id"],)
            )
        return {"status": "WITHDRAWN", "intent_id": row["intent_id"]}

    def result_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT intent.*, receipt.response_json, receipt.evidence_refs_json,
                       receipt.created_at AS receipt_created_at
                FROM external_intents AS intent
                JOIN receipts AS receipt ON receipt.intent_id = intent.intent_id
                WHERE intent.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        intent = _intent_row(row)
        receipt = {
            "intent_id": row["intent_id"],
            "response": json.loads(row["response_json"]),
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "created_at": row["receipt_created_at"],
        }
        return {"operation": row["operation"], "intent": intent, "receipt": receipt}

    def receipt(
        self,
        intent_id: str,
        response: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        evidence_refs = validate_evidence_refs(evidence_refs)
        evidence_json = json.dumps(
            evidence_refs, ensure_ascii=False, separators=(",", ":")
        )
        response_json = _encode(response)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT * FROM external_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise ValueError("INTENT_NOT_FOUND")
            existing = connection.execute(
                "SELECT * FROM receipts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["response_json"] != response_json
                    or existing["evidence_refs_json"] != evidence_json
                ):
                    raise ValueError("RECEIPT_CONFLICT")
                return _receipt_row(existing)
            created_at = _now()
            connection.execute(
                """
                INSERT INTO receipts(intent_id, response_json, evidence_refs_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (intent_id, response_json, evidence_json, created_at),
            )
            connection.execute(
                """
                INSERT INTO external_results(
                    intent_id, run_id, operation, result_json, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    intent["run_id"],
                    intent["operation"],
                    response_json,
                    evidence_json,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM receipts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return _receipt_row(row)

    def abandon_intent(self, intent_id: str, reason: str, actor: str) -> dict[str, Any]:
        """Stop waiting on an unreconciled external write, on the record.

        The x86bgw CDN-URL run had no supported way to do this, so it was done three
        times with `DELETE FROM external_intents` / `receipts` / `external_results`
        against the live database. Deleting the row is the one outcome that must not
        happen: the intent log is the only account of what this control plane asked the
        outside world to do, and a run that unsticks itself by erasing that account has
        traded a stuck run for an audit trail with a hole in it.

        So this writes instead of deleting. The abandonment is a receipt like any other
        -- `ok: False`, reason `INTENT_ABANDONED`, carrying who decided and why -- which
        clears the intent out of `pending_intents` (unblocking the run) while leaving it
        in `external_results`, in `trace`, and in the run summary's failure groups,
        where `_failure_groups` picks up any `ok: False` response without needing to
        know this code exists.

        It is deliberately not an approval-gated action. The intents that strand a run
        are frequently the approval deliveries themselves, and a gate that needs the
        stuck channel to open it is not an escape hatch. `reason` and `actor` are
        required in its place: an unexplained abandonment is not auditable either.

        `withdraw_intent` stays the right call for the narrow in-process case where a
        client refused before dispatching, because there the write provably never
        happened and there is nothing to account for. Here the outcome is unknown and
        stays recorded as unknown.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("ABANDON_REASON_REQUIRED")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("ABANDON_ACTOR_REQUIRED")
        # Deterministic, so abandoning twice with the same reason replays rather than
        # colliding with itself. A timestamp lives on the receipt row instead.
        response = {
            "ok": False, "reason_code": "INTENT_ABANDONED",
            "abandoned": True, "reason": reason.strip(), "actor": actor.strip(),
        }
        ensure_persistable(response)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT * FROM external_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise ValueError("INTENT_NOT_FOUND")
            existing = connection.execute(
                "SELECT * FROM receipts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is not None:
                stored = json.loads(existing["response_json"])
                if not (isinstance(stored, dict) and stored.get("abandoned") is True):
                    raise ValueError("INTENT_ALREADY_RECONCILED")
                if stored != response:
                    raise ValueError("ABANDON_CONFLICT")
                return {
                    "status": "ALREADY_ABANDONED",
                    "intent": _intent_row(intent),
                    "receipt": _receipt_row(existing),
                }
            response_json = _encode(response)
            created_at = _now()
            connection.execute(
                "INSERT INTO receipts(intent_id, response_json, evidence_refs_json, created_at)"
                " VALUES (?, ?, '[]', ?)",
                (intent_id, response_json, created_at),
            )
            connection.execute(
                """
                INSERT INTO external_results(
                    intent_id, run_id, operation, result_json, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, '[]', ?)
                """,
                (intent_id, intent["run_id"], intent["operation"], response_json, created_at),
            )
            row = connection.execute(
                "SELECT * FROM receipts WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return {
            "status": "ABANDONED",
            "intent": _intent_row(intent),
            "receipt": _receipt_row(row),
        }

    def pending_intents(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT intent.*
                FROM external_intents AS intent
                LEFT JOIN receipts ON receipts.intent_id = intent.intent_id
                WHERE intent.run_id = ? AND receipts.intent_id IS NULL
                ORDER BY intent.sequence
                """,
                (run_id,),
            ).fetchall()
        return [_intent_row(row) for row in rows]

    def external_results(self, run_id: str) -> list[dict[str, Any]]:
        """Return durable, verified side-effect evidence in intent order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT intent.intent_id, intent.run_id, intent.operation, intent.idempotency_key,
                       intent.payload_json, intent.created_at AS intent_created_at,
                       receipt.response_json, receipt.evidence_refs_json,
                       receipt.created_at AS receipt_created_at
                FROM external_intents AS intent
                JOIN receipts AS receipt ON receipt.intent_id = intent.intent_id
                WHERE intent.run_id = ?
                ORDER BY intent.sequence
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "intent": {
                    "intent_id": row["intent_id"], "run_id": row["run_id"],
                    "operation": row["operation"], "idempotency_key": row["idempotency_key"],
                    "payload": json.loads(row["payload_json"]), "created_at": row["intent_created_at"],
                },
                "receipt": {
                    "intent_id": row["intent_id"], "response": json.loads(row["response_json"]),
                    "evidence_refs": json.loads(row["evidence_refs_json"]),
                    "created_at": row["receipt_created_at"],
                },
            }
            for row in rows
        ]

    def record_handoff(
        self, run_id: str, handoff_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        encoded = _encode(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != run_id or existing["payload_json"] != encoded:
                    raise ValueError("HANDOFF_CONFLICT")
                return _handoff_row(existing)
            created_at = _now()
            connection.execute(
                """
                INSERT INTO handoffs(
                    handoff_id, run_id, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (handoff_id, run_id, encoded, created_at, created_at),
            )
            row = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        return _handoff_row(row)

    def complete_handoff(self, handoff_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
            if row is None:
                raise ValueError("HANDOFF_NOT_FOUND")
            if row["status"] != "COMPLETE":
                connection.execute(
                    "UPDATE handoffs SET status = 'COMPLETE', updated_at = ? WHERE handoff_id = ?",
                    (_now(), handoff_id),
                )
                row = connection.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
        return _handoff_row(row)

    def incomplete_handoffs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM handoffs
                WHERE run_id = ? AND status != 'COMPLETE'
                ORDER BY handoff_id
                """,
                (run_id,),
            ).fetchall()
        return [_handoff_row(row) for row in rows]

    def record_producer_job(
        self, run_id: str, job_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Enqueue a request for model-authored DraftContent for one phase.

        Idempotent on job_id: a replay with the same run/payload returns the existing
        row; a conflicting replay raises, so a re-issued `next` cannot fork the job.
        """
        encoded = _encode(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM producer_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != run_id or existing["payload_json"] != encoded:
                    raise ValueError("PRODUCER_JOB_CONFLICT")
                return _producer_job_row(existing)
            created_at = _now()
            connection.execute(
                """
                INSERT INTO producer_jobs(
                    job_id, run_id, payload_json, status, draft_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', NULL, ?, ?)
                """,
                (job_id, run_id, encoded, created_at, created_at),
            )
            row = connection.execute(
                "SELECT * FROM producer_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _producer_job_row(row)

    def fulfill_producer_job(self, job_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        """Record the DraftContent a producer returned; idempotent on identical draft."""
        encoded = _encode(draft)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM producer_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("PRODUCER_JOB_NOT_FOUND")
            if row["status"] == "FULFILLED":
                if row["draft_json"] != encoded:
                    raise ValueError("PRODUCER_JOB_CONFLICT")
                return _producer_job_row(row)
            connection.execute(
                "UPDATE producer_jobs SET status = 'FULFILLED', draft_json = ?, updated_at = ? WHERE job_id = ?",
                (encoded, _now(), job_id),
            )
            row = connection.execute(
                "SELECT * FROM producer_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _producer_job_row(row)

    def producer_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM producer_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _producer_job_row(row) if row is not None else None

    def pending_producer_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM producer_jobs
                WHERE run_id = ? AND status = 'PENDING'
                ORDER BY job_id
                """,
                (run_id,),
            ).fetchall()
        return [_producer_job_row(row) for row in rows]

    def record_model_execution_receipt(
        self, run_id: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        """Record how one ProducerJob's DraftContent was produced (provider/model/version/
        prompt_version/skill_version/temperature/seed/input_hash/output_hash/validators).

        Idempotent on (job_id, output_hash): the same fill recorded twice returns the
        existing row; a different receipt for that identity raises. This is the ledger a
        later golden replay and cross-model diff gate read from.
        """
        job_id = receipt.get("job_id")
        output_hash = receipt.get("output_hash")
        if not isinstance(job_id, str) or not job_id or not isinstance(output_hash, str) or not output_hash:
            raise ValueError("MODEL_RECEIPT_INVALID")
        receipt_id = hashlib.sha256(f"{job_id}\0{output_hash}".encode("utf-8")).hexdigest()
        encoded = _encode(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM model_execution_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != run_id or existing["receipt_json"] != encoded:
                    raise ValueError("MODEL_RECEIPT_CONFLICT")
                return _model_execution_receipt_row(existing)
            connection.execute(
                """
                INSERT INTO model_execution_receipts(
                    receipt_id, run_id, job_id, phase, input_hash, output_hash,
                    receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, run_id, job_id, receipt.get("phase"), receipt.get("input_hash"),
                 output_hash, encoded, _now()),
            )
            row = connection.execute(
                "SELECT * FROM model_execution_receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
        return _model_execution_receipt_row(row)

    def model_execution_receipts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM model_execution_receipts
                WHERE run_id = ? ORDER BY created_at, receipt_id
                """,
                (run_id,),
            ).fetchall()
        return [_model_execution_receipt_row(row) for row in rows]

    def cache_draft(
        self, input_hash: str, prompt_version: str, model: str | None, draft: dict[str, Any]
    ) -> dict[str, Any]:
        """Cache a producer's DraftContent under (input_hash, prompt_version, model).

        Insert-if-absent: the cache is authoritative and immutable per key, so a hit
        returns the stored draft (`cached: True`) and the incoming one is ignored — a
        worker re-driving the same frontier with the same prompt/model reuses the draft
        instead of re-invoking the producer. `model` is None for the agent-turn backend;
        a real model id later keys distinct entries, which is what a cross-model diff reads.
        """
        model_key = model or ""
        cache_key = hashlib.sha256(
            f"{input_hash}\0{prompt_version}\0{model_key}".encode("utf-8")
        ).hexdigest()
        output_hash = hashlib.sha256(_encode(draft).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM draft_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if existing is not None:
                return {**_draft_cache_row(existing), "cached": True}
            connection.execute(
                """
                INSERT INTO draft_cache(
                    cache_key, input_hash, prompt_version, model, output_hash,
                    draft_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cache_key, input_hash, prompt_version, model_key, output_hash,
                 _encode(draft), _now()),
            )
            row = connection.execute(
                "SELECT * FROM draft_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return {**_draft_cache_row(row), "cached": False}

    def cached_draft(
        self, input_hash: str, prompt_version: str, model: str | None
    ) -> dict[str, Any] | None:
        model_key = model or ""
        cache_key = hashlib.sha256(
            f"{input_hash}\0{prompt_version}\0{model_key}".encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return _draft_cache_row(row) if row is not None else None

    def save_optimization_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Persist one immutable G10 candidate and its mutable result status."""
        required = ("proposal_id", "run_id", "candidate_hash", "envelope_hash")
        if any(not isinstance(proposal.get(key), str) or not proposal[key] for key in required):
            raise ValueError("OPTIMIZATION_PROPOSAL_INVALID")
        encoded = _encode(proposal)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM optimization_proposals WHERE proposal_id = ?",
                (proposal["proposal_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != encoded or existing["candidate_hash"] != proposal["candidate_hash"] or existing["envelope_hash"] != proposal["envelope_hash"]:
                    raise ValueError("OPTIMIZATION_PROPOSAL_CONFLICT")
                return _optimization_row(existing)
            connection.execute(
                """INSERT INTO optimization_proposals(
                    proposal_id, run_id, candidate_hash, envelope_hash, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ARCHIVING', ?, ?)""",
                (proposal["proposal_id"], proposal["run_id"], proposal["candidate_hash"], proposal["envelope_hash"], encoded, now, now),
            )
            row = connection.execute(
                "SELECT * FROM optimization_proposals WHERE proposal_id = ?",
                (proposal["proposal_id"],),
            ).fetchone()
        return _optimization_row(row)

    def mark_optimization_archived(self, proposal_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None: raise ValueError("OPTIMIZATION_PROPOSAL_NOT_FOUND")
            if row["status"] == "PROPOSED" and row["proposal_receipt_json"] == encoded: return _optimization_row(row)
            if row["status"] != "ARCHIVING": raise ValueError("OPTIMIZATION_ARCHIVE_STATE_INVALID")
            connection.execute("UPDATE optimization_proposals SET status='PROPOSED', proposal_receipt_json=?, archive_receipt_json=?, updated_at=? WHERE proposal_id=?", (encoded, encoded, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def claim_optimization_apply(self, proposal_id: str, approval_id: str, journal: dict[str, Any]) -> dict[str, Any]:
        """CAS PROPOSED -> APPLYING after a complete backup journal is durable."""
        token = uuid.uuid4().hex
        heartbeat_at = datetime.now(timezone.utc)
        lease_expires_at = (heartbeat_at + timedelta(seconds=300)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if row is None:
                return {"status": "NOT_FOUND"}
            if row["status"] != "PROPOSED":
                return {"status": row["status"], "proposal": _optimization_row(row)}
            proposal = json.loads(row["payload_json"])
            expected = {
                "schema_version": "2", "proposal_id": proposal_id, "run_id": row["run_id"],
                "candidate_hash": row["candidate_hash"], "approval_id": approval_id,
                "allowed_roots": proposal.get("allowed_roots"),
                "target_identities": [
                    {"path": item["path"], "before_sha256": item["before_sha256"], "content_sha256": item["content_sha256"]}
                    for item in proposal.get("candidate", {}).get("target_files", [])
                ],
            }
            if not _claim_journal_matches(journal, expected):
                return {"status": "JOURNAL_INVALID"}
            journal = dict(journal)
            # The journal stores a non-secret ownership proof; the actual token remains in its dedicated column.
            journal["claim"] = {"owner_proof": hashlib.sha256(token.encode("utf-8")).hexdigest(), "owner_pid": os.getpid(), "heartbeat_at": heartbeat_at.isoformat(), "lease_expires_at": lease_expires_at}
            journal["journal_hash"] = _journal_hash({key: value for key, value in journal.items() if key != "journal_hash"})
            encoded = _encode(journal)
            connection.execute(
                """UPDATE optimization_proposals SET status = 'APPLYING', approval_id = ?, owner_token = ?,
                   owner_pid = ?, apply_journal_json = ?, heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                   WHERE proposal_id = ? AND status = 'PROPOSED'""",
                (approval_id, token, os.getpid(), encoded, heartbeat_at.isoformat(), lease_expires_at, _now(), proposal_id),
            )
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return {"status": "CLAIMED", "owner_token": token, "proposal": _optimization_row(row)}

    def heartbeat_optimization_apply(self, proposal_id: str, owner_token: str, lease_seconds: int = 300) -> dict[str, Any]:
        if not isinstance(lease_seconds, int) or lease_seconds <= 0: raise ValueError("OPTIMIZATION_LEASE_INVALID")
        now = datetime.now(timezone.utc); expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
            if row is None or row["status"] != "APPLYING" or row["owner_token"] != owner_token: raise ValueError("OPTIMIZATION_OWNER_MISMATCH")
            connection.execute("UPDATE optimization_proposals SET heartbeat_at=?, lease_expires_at=?, updated_at=? WHERE proposal_id=?",(now.isoformat(),expiry,_now(),proposal_id))
            row=connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
        return _optimization_row(row)

    def finalize_optimization_apply(self, proposal_id: str, owner_token: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if row is None or row["status"] not in {"APPLYING", "RECOVERY_REQUIRED"} or row["owner_token"] != owner_token:
                raise ValueError("OPTIMIZATION_OWNER_MISMATCH")
            connection.execute("UPDATE optimization_proposals SET status=?, result_json=?, updated_at=? WHERE proposal_id=?", (status, encoded, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def begin_optimization_result_archive(self, proposal_id: str, owner_token: str, result: dict[str, Any], terminal_status: str) -> dict[str, Any]:
        """Persist the result before publishing it; terminal states require a receipt."""
        if terminal_status not in {"APPLIED", "ROLLED_BACK"}:
            raise ValueError("OPTIMIZATION_TERMINAL_STATUS_INVALID")
        encoded = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["status"] not in {"APPLYING", "RECOVERY_REQUIRED"} or row["owner_token"] != owner_token:
                raise ValueError("OPTIMIZATION_OWNER_MISMATCH")
            connection.execute("UPDATE optimization_proposals SET status='RESULT_ARCHIVING', result_json=?, pending_terminal_status=?, updated_at=? WHERE proposal_id=?", (encoded, terminal_status, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def mark_optimization_result_archive_pending(self, proposal_id: str, result: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["status"] not in {"RESULT_ARCHIVING", "ARCHIVE_PENDING"}:
                raise ValueError("OPTIMIZATION_RESULT_ARCHIVE_STATE_INVALID")
            connection.execute("UPDATE optimization_proposals SET status='ARCHIVE_PENDING', result_json=?, updated_at=? WHERE proposal_id=?", (encoded, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def complete_optimization_result_archive(self, proposal_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["status"] not in {"RESULT_ARCHIVING", "ARCHIVE_PENDING"} or row["pending_terminal_status"] not in {"APPLIED", "ROLLED_BACK"}:
                raise ValueError("OPTIMIZATION_RESULT_ARCHIVE_STATE_INVALID")
            connection.execute("UPDATE optimization_proposals SET status=?, result_receipt_json=?, pending_terminal_status=NULL, updated_at=? WHERE proposal_id=?", (row["pending_terminal_status"], encoded, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def mark_optimization_recovery_required(self, proposal_id: str, owner_token: str | None, result: dict[str, Any]) -> dict[str, Any]:
        encoded = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["status"] not in {"APPLYING", "ROLLBACK_FAILED", "RECOVERY_REQUIRED"} or (owner_token is not None and row["owner_token"] != owner_token):
                raise ValueError("OPTIMIZATION_OWNER_MISMATCH")
            connection.execute("UPDATE optimization_proposals SET status='RECOVERY_REQUIRED', result_json=?, updated_at=? WHERE proposal_id=?", (encoded, _now(), proposal_id))
            row = connection.execute("SELECT * FROM optimization_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return _optimization_row(row)

    def optimization_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return _optimization_row(row) if row is not None else None

    def update_optimization_proposal(
        self, proposal_id: str, status: str, result: dict[str, Any], approval_id: str | None = None
    ) -> dict[str, Any]:
        encoded = _encode(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ValueError("OPTIMIZATION_PROPOSAL_NOT_FOUND")
            if row["status"] == status and row["result_json"] == encoded:
                return _optimization_row(row)
            if row["status"] in {"APPLIED", "ROLLED_BACK", "REJECTED", "ARCHIVE_FAILED"}:
                if row["status"] != status:
                    raise ValueError("OPTIMIZATION_PROPOSAL_FINAL")
            connection.execute(
                "UPDATE optimization_proposals SET status = ?, result_json = ?, approval_id = COALESCE(?, approval_id), updated_at = ? WHERE proposal_id = ?",
                (status, encoded, approval_id, _now(), proposal_id),
            )
            row = connection.execute(
                "SELECT * FROM optimization_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return _optimization_row(row)


def _encode(value: dict[str, Any]) -> str:
    ensure_persistable(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _journal_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim_journal_matches(journal: Any, expected: dict[str, Any]) -> bool:
    try:
        if not isinstance(journal, dict) or any(journal.get(key) != value for key, value in expected.items()): return False
        entries = journal.get("entries")
        targets = expected["target_identities"]
        if not isinstance(entries, list) or len(entries) != len(targets): return False
        for entry, target in zip(entries, targets):
            data = base64.b64decode(entry["bytes_b64"], validate=True)
            if entry.get("path") != target["path"] or entry.get("sha256") != hashlib.sha256(data).hexdigest() or entry["sha256"] != target["before_sha256"]: return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _superseded_external_receipt(response: Any) -> bool:
    """Whether a receipt closes an attempt without proving the write landed.

    Abandonment is one such close. A local SUBMIT_REJECTED that never created a CR
    is another: replaying it forever would hide a later skill fix behind the first
    failed push_cr.
    """
    if not isinstance(response, dict):
        return False
    if response.get("abandoned") is True:
        return True
    return (
        response.get("ok") is False
        and response.get("reason_code") == "SUBMIT_REJECTED"
        and not response.get("change_number")
    )


def _intent_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "intent_id": row["intent_id"],
        "run_id": row["run_id"],
        "operation": row["operation"],
        "idempotency_key": row["idempotency_key"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    }


def _receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "intent_id": row["intent_id"],
        "response": json.loads(row["response_json"]),
        "evidence_refs": json.loads(row["evidence_refs_json"]),
        "created_at": row["created_at"],
    }


def _handoff_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "handoff_id": row["handoff_id"],
        "run_id": row["run_id"],
        "payload": json.loads(row["payload_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _producer_job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "run_id": row["run_id"],
        "payload": json.loads(row["payload_json"]),
        "status": row["status"],
        "draft": json.loads(row["draft_json"]) if row["draft_json"] is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _model_execution_receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "receipt_id": row["receipt_id"],
        "run_id": row["run_id"],
        "job_id": row["job_id"],
        "created_at": row["created_at"],
        **json.loads(row["receipt_json"]),
    }


def _draft_cache_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cache_key": row["cache_key"],
        "input_hash": row["input_hash"],
        "prompt_version": row["prompt_version"],
        "model": row["model"] or None,
        "output_hash": row["output_hash"],
        "draft": json.loads(row["draft_json"]),
        "created_at": row["created_at"],
    }


def _optimization_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": row["proposal_id"],
        "run_id": row["run_id"],
        "candidate_hash": row["candidate_hash"],
        "envelope_hash": row["envelope_hash"],
        "proposal": json.loads(row["payload_json"]),
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "approval_id": row["approval_id"],
        "owner_token": row["owner_token"],
        "owner_pid": row["owner_pid"],
        "apply_journal": json.loads(row["apply_journal_json"]) if row["apply_journal_json"] else None,
        "archive_receipt": json.loads(row["proposal_receipt_json"] or row["archive_receipt_json"]) if (row["proposal_receipt_json"] or row["archive_receipt_json"]) else None,
        "proposal_receipt": json.loads(row["proposal_receipt_json"]) if row["proposal_receipt_json"] else None,
        "result_receipt": json.loads(row["result_receipt_json"]) if row["result_receipt_json"] else None,
        "pending_terminal_status": row["pending_terminal_status"],
        "heartbeat_at": row["heartbeat_at"], "lease_expires_at": row["lease_expires_at"],
    }


def _optimization_migrate(connection: sqlite3.Connection) -> None:
    fields = {
        "envelope_hash": "TEXT NOT NULL DEFAULT ''", "approval_id": "TEXT",
        "owner_token": "TEXT", "owner_pid": "INTEGER", "apply_journal_json": "TEXT", "archive_receipt_json": "TEXT", "proposal_receipt_json": "TEXT", "result_receipt_json": "TEXT", "pending_terminal_status": "TEXT", "heartbeat_at": "TEXT", "lease_expires_at": "TEXT",
    }
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(optimization_proposals)")}
    for field, definition in fields.items():
        if field not in existing:
            connection.execute(f"ALTER TABLE optimization_proposals ADD COLUMN {field} {definition}")
