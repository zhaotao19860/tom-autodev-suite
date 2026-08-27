from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


class LockManager:
    def __init__(
        self,
        database_path: Path | str,
        *,
        pid: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid = os.getpid() if pid is None else pid
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.executescript(
                """
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
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def acquire(self, key: str, owner_token: str, ttl_seconds: int) -> dict[str, Any]:
        if not key or not owner_token or ttl_seconds <= 0:
            raise ValueError("LOCK_ARGUMENT_INVALID")
        now = self.clock().astimezone(timezone.utc)
        now_text = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM locks WHERE lock_key = ?", (key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO locks(
                        lock_key, owner_token, owner_pid, ttl_seconds, created_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, owner_token, self.pid, ttl_seconds, now_text, now_text),
                )
                self._record_heartbeat(connection, key, owner_token, self.pid, now_text, "ACQUIRE")
                return _acquired(key, owner_token, self.pid, ttl_seconds, now_text)

            stale = _is_stale(existing, now)
            if not stale:
                return {
                    "key": key,
                    "acquired": False,
                    "takeover": False,
                    "reason_code": "LOCK_ACTIVE",
                    "owner_pid": existing["owner_pid"],
                }

            previous_owner = existing["owner_token"]
            previous_pid = existing["owner_pid"]
            connection.execute(
                """
                UPDATE locks
                SET owner_token = ?, owner_pid = ?, ttl_seconds = ?,
                    created_at = ?, heartbeat_at = ?
                WHERE lock_key = ?
                """,
                (owner_token, self.pid, ttl_seconds, now_text, now_text, key),
            )
            self._record_heartbeat(connection, key, owner_token, self.pid, now_text, "TAKEOVER")
            result = _acquired(key, owner_token, self.pid, ttl_seconds, now_text)
            result.update(
                {
                    "takeover": True,
                    "previous_owner_token": previous_owner,
                    "previous_owner_pid": previous_pid,
                }
            )
            return result

    def heartbeat(self, key: str, owner_token: str) -> dict[str, Any]:
        now_text = self.clock().astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM locks WHERE lock_key = ?", (key,)
            ).fetchone()
            if existing is None:
                raise ValueError("LOCK_NOT_FOUND")
            if existing["owner_token"] != owner_token or existing["owner_pid"] != self.pid:
                raise ValueError("LOCK_OWNER_MISMATCH")
            connection.execute(
                "UPDATE locks SET heartbeat_at = ? WHERE lock_key = ?",
                (now_text, key),
            )
            self._record_heartbeat(
                connection, key, owner_token, self.pid, now_text, "HEARTBEAT"
            )
        return {"key": key, "status": "HEARTBEAT_RECORDED", "heartbeat_at": now_text}

    def records(self) -> dict[str, list[dict[str, Any]]]:
        now = self.clock().astimezone(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM locks ORDER BY lock_key").fetchall()
        active = []
        stale = []
        for row in rows:
            record = _lock_row(row)
            (stale if _is_stale(row, now) else active).append(record)
        return {"active": active, "stale": stale}

    @staticmethod
    def _record_heartbeat(
        connection: sqlite3.Connection,
        key: str,
        owner_token: str,
        pid: int,
        timestamp: str,
        kind: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO heartbeats(
                lock_key, owner_token, owner_pid, heartbeat_at, kind
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (key, owner_token, pid, timestamp, kind),
        )


def _is_stale(row: sqlite3.Row, now: datetime) -> bool:
    heartbeat = datetime.fromisoformat(row["heartbeat_at"])
    expired = now >= heartbeat + timedelta(seconds=row["ttl_seconds"])
    return expired and not _pid_alive(row["owner_pid"])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquired(
    key: str, owner_token: str, pid: int, ttl_seconds: int, created_at: str
) -> dict[str, Any]:
    return {
        "key": key,
        "owner_token": owner_token,
        "pid": pid,
        "ttl_seconds": ttl_seconds,
        "created_at": created_at,
        "heartbeat_at": created_at,
        "acquired": True,
        "takeover": False,
        "reason_code": None,
    }


def _lock_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["lock_key"],
        "owner_token": row["owner_token"],
        "pid": row["owner_pid"],
        "ttl_seconds": row["ttl_seconds"],
        "created_at": row["created_at"],
        "heartbeat_at": row["heartbeat_at"],
    }
