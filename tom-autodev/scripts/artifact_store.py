from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from persistence_policy import ensure_persistable, validate_evidence_refs
from schema_validator import validate_named_schema


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FINAL_ENVELOPE_KEYS = frozenset({
    "action_id", "source_event_id", "host", "run_id", "phase", "task_id",
    "schema_version", "input_hash", "content_hash", "source_revisions",
    "parent_artifact_hash", "knowledge_doc_id", "knowledge_url", "knowledge_version",
    "icafe_comment_id", "evidence_refs", "approval_id", "approval_input_hash", "content",
})
_PHASE_SCHEMAS = {
    "INTAKE": "requirement-snapshot", "GRILL": "decision-log", "SPEC": "spec",
    "TASKS": "task-dag", "PLAN": "task-plan", "IMPLEMENT": "change-set",
    "REVIEW": "review", "DIAGNOSE": "diagnosis", "IPIPE": "ipipe-evidence",
}
# Content checks for the kinds written through the generic `put`. `put_envelope` picks
# its schema from the phase, so phase artifacts were always validated; everything else
# was archived, hashed, indexed, and then read back as evidence without anyone having
# looked at its shape. A submit descriptor missing `revision_set` got as far as iCode
# rejecting it, and a run summary in a shape G10 does not understand was still the
# document G10 reasoned from.
#
# `change-set` here is the submit descriptor, NOT the IMPLEMENT phase artifact: that
# one is stored under kind `implement`. Mapping each kind to a same-named schema is
# exactly the mistake this table exists to avoid -- it would check the descriptor
# against `change-set.schema.json` and reject every SUBMIT.
_KIND_SCHEMAS = {
    "change-set": "submit-descriptor",
    "run-summary": "run-summary",
}
# Kinds archived unchecked on purpose, so an absent kind can be told from a considered
# exemption. Both wrap a remote response verbatim: their shape belongs to iCode, and
# pinning it here would reject real evidence for being unfamiliar. What the controller
# needs from them is checked where it is read, against the request that produced it
# (`_submission_controller_binding`, `clients/icode_ai_review`).
#
# An unlisted kind is still archived. Turning this into an allowlist would be a
# stronger guarantee and a different change: the store is also used for opaque bytes,
# which have no schema to name.
_UNVALIDATED_KINDS = frozenset({"submission", "ai-review"})


class ArtifactStore:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "artifact-index.sqlite"
        with self._connect() as connection:
            connection.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS phase_artifacts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    artifact_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS phase_artifacts_run_sequence
                    ON phase_artifacts(run_id, sequence);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # `with sqlite3.connect(...)` ends the transaction and leaves the handle open;
        # closing here is what stops one leaked descriptor per index read.
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(
        self,
        run_id: str,
        kind: str,
        content: bytes,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        ensure_persistable(metadata)
        _validate_component(run_id)
        _validate_component(kind)
        _validate_kind_content(kind, content)
        content_hash = hashlib.sha256(content).hexdigest()
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = "\0".join((run_id, kind, content_hash, metadata_json)).encode("utf-8")
        artifact_id = hashlib.sha256(identity).hexdigest()
        artifact_dir = self.root / run_id / kind / artifact_id
        content_path = artifact_dir / "content.bin"
        metadata_path = artifact_dir / "metadata.json"
        metadata_bytes = metadata_json.encode("utf-8")
        metadata_hash = hashlib.sha256(metadata_bytes).hexdigest()
        _require_confined(artifact_dir, self.root)
        _require_confined(content_path, self.root)
        _require_confined(metadata_path, self.root)
        expected = {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "kind": kind,
            "content_path": str(content_path),
            "content_sha256": content_hash,
            "metadata_path": str(metadata_path),
            "metadata_sha256": metadata_hash,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if existing is not None:
                if not _matches(existing, expected) or not self._load(existing).get("valid"):
                    raise ValueError("ARTIFACT_CONFLICT")
                return _descriptor(artifact_id, run_id, kind, content_hash, content_path, metadata)

            directory_existed = artifact_dir.exists()
            try:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                _require_confined(artifact_dir, self.root)
                _require_confined(content_path, self.root)
                _require_confined(metadata_path, self.root)
                _write_once(content_path, content)
                _write_once(metadata_path, metadata_bytes)
            except Exception:
                if not directory_existed:
                    shutil.rmtree(artifact_dir, ignore_errors=True)
                raise
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, run_id, kind, content_path, content_sha256,
                    metadata_path, metadata_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    str(content_path),
                    content_hash,
                    str(metadata_path),
                    metadata_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return _descriptor(artifact_id, run_id, kind, content_hash, content_path, metadata)

    def get(self, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            return _invalid(artifact_id, "ARTIFACT_NOT_FOUND")
        return self._load(row)

    def put_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Prepare immutable files, then insert artifact and phase index in one transaction."""
        _validate_final_envelope(envelope)
        action_id, run_id, phase = (envelope[key] for key in ("action_id", "run_id", "phase"))
        task_id = envelope["task_id"]
        content_hash = envelope["content_hash"]
        content = _canonical_json(envelope).encode("utf-8")
        kind = phase.lower().replace("_", "-")
        _validate_component(run_id)
        _validate_component(kind)
        metadata = {
            "action_id": action_id, "phase": phase, "task_id": task_id,
            "content_hash": content_hash, "schema_version": envelope.get("schema_version"),
            "schema_name": _PHASE_SCHEMAS[phase],
        }
        metadata_json = _canonical_json(metadata)
        content_file_hash = hashlib.sha256(content).hexdigest()
        identity = "\0".join((run_id, kind, content_file_hash, metadata_json)).encode("utf-8")
        artifact_id = hashlib.sha256(identity).hexdigest()
        artifact_dir = self.root / run_id / kind / artifact_id
        content_path = artifact_dir / "content.bin"
        metadata_path = artifact_dir / "metadata.json"
        metadata_bytes = metadata_json.encode("utf-8")
        metadata_hash = hashlib.sha256(metadata_bytes).hexdigest()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _require_confined(artifact_dir, self.root)
        _require_confined(content_path, self.root)
        _require_confined(metadata_path, self.root)
        _write_once(content_path, content)
        _write_once(metadata_path, metadata_bytes)
        task_key = task_id or ""
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_phase = connection.execute(
                "SELECT * FROM phase_artifacts WHERE action_id = ?", (action_id,)
            ).fetchone()
            expected_phase = (run_id, phase, task_key, content_hash, artifact_id)
            if existing_phase is not None:
                actual = tuple(existing_phase[key] for key in ("run_id", "phase", "task_key", "content_hash", "artifact_id"))
                if actual != expected_phase:
                    raise ValueError("ARTIFACT_CONFLICT")
            else:
                existing_artifact = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                expected_artifact = {
                    "artifact_id": artifact_id, "run_id": run_id, "kind": kind,
                    "content_path": str(content_path), "content_sha256": content_file_hash,
                    "metadata_path": str(metadata_path), "metadata_sha256": metadata_hash,
                }
                if existing_artifact is not None and not _matches(existing_artifact, expected_artifact):
                    raise ValueError("ARTIFACT_CONFLICT")
                if existing_artifact is None:
                    connection.execute(
                        """
                        INSERT INTO artifacts(
                            artifact_id, run_id, kind, content_path, content_sha256,
                            metadata_path, metadata_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (artifact_id, run_id, kind, str(content_path), content_file_hash,
                         str(metadata_path), metadata_hash, created_at),
                    )
                connection.execute(
                    """
                    INSERT INTO phase_artifacts(
                        action_id, run_id, phase, task_key, content_hash, artifact_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (action_id, *expected_phase, created_at),
                )
        descriptor = _descriptor(artifact_id, run_id, kind, content_file_hash, content_path, metadata)
        return {**descriptor, "envelope": json.loads(content), "valid": True, "reason_code": None}

    def latest_phase(
        self, run_id: str, phase: str | None = None, task_id: str | None = None
    ) -> dict[str, Any]:
        clauses = ["run_id = ?"]
        parameters: list[Any] = [run_id]
        if phase is not None:
            clauses.append("phase = ?")
            parameters.append(phase)
        if task_id is not None:
            clauses.append("task_key = ?")
            parameters.append(task_id)
        query = f"SELECT * FROM phase_artifacts WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return _invalid("", "ARTIFACT_NOT_FOUND")
        return self._load_phase(row)

    def phase_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM phase_artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            return _invalid(artifact_id, "ARTIFACT_NOT_FOUND")
        return self._load_phase(row)

    def phase_artifacts(self, run_id: str, phase: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM phase_artifacts WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if phase is not None:
            query += " AND phase = ?"
            parameters.append(phase)
        query += " ORDER BY sequence"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._load_phase(row) for row in rows]

    def artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """List only integrity-checked artifacts so summaries never trust the index alone."""
        with self._connect() as connection:
            rows = connection.execute(
                # `rowid` rather than `artifact_id` as the tiebreaker: callers read the
                # last artifact of a kind as the current one, and `created_at` has
                # microsecond resolution, so two puts inside the same microsecond would
                # otherwise be ordered by a content hash — an arbitrary answer to
                # "which of these two change sets is the one to submit".
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
            ).fetchall()
        return [self._load(row) for row in rows]

    def _load_phase(self, row: sqlite3.Row) -> dict[str, Any]:
        loaded = self.get(row["artifact_id"])
        if not loaded.get("valid"):
            return loaded
        try:
            envelope = json.loads(loaded["content"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _invalid(row["artifact_id"], "ARTIFACT_CONTENT_INVALID")
        if (
            not isinstance(envelope, dict)
            or envelope.get("content_hash") != row["content_hash"]
            or _envelope_hash(envelope) != row["content_hash"]
        ):
            return _invalid(row["artifact_id"], "ARTIFACT_CONTENT_HASH_MISMATCH")
        if (
            envelope.get("action_id") != row["action_id"]
            or envelope.get("run_id") != row["run_id"]
            or envelope.get("phase") != row["phase"]
            or (envelope.get("task_id") or "") != row["task_key"]
        ):
            return _invalid(row["artifact_id"], "ARTIFACT_INDEX_MISMATCH")
        try:
            _validate_final_envelope(envelope)
        except ValueError as error:
            return _invalid(row["artifact_id"], str(error))
        return {**loaded, "envelope": envelope}

    def _load(self, row: sqlite3.Row) -> dict[str, Any]:
        artifact_id = row["artifact_id"]
        try:
            content_path = _require_confined(Path(row["content_path"]), self.root)
            metadata_path = _require_confined(Path(row["metadata_path"]), self.root)
        except ValueError:
            return _invalid(artifact_id, "ARTIFACT_PATH_ESCAPE")
        try:
            content = content_path.read_bytes()
        except OSError:
            return _invalid(artifact_id, "ARTIFACT_CONTENT_MISSING")
        if hashlib.sha256(content).hexdigest() != row["content_sha256"]:
            return _invalid(artifact_id, "ARTIFACT_CONTENT_HASH_MISMATCH")
        try:
            metadata_bytes = metadata_path.read_bytes()
        except OSError:
            return _invalid(artifact_id, "ARTIFACT_METADATA_MISSING")
        if hashlib.sha256(metadata_bytes).hexdigest() != row["metadata_sha256"]:
            return _invalid(artifact_id, "ARTIFACT_METADATA_HASH_MISMATCH")
        try:
            metadata = json.loads(metadata_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _invalid(artifact_id, "ARTIFACT_METADATA_INVALID")
        return {
            "artifact_id": artifact_id,
            "run_id": row["run_id"],
            "kind": row["kind"],
            "sha256": row["content_sha256"],
            "path": str(content_path),
            "metadata": metadata,
            "content": content,
            "valid": True,
            "reason_code": None,
        }


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as error:
            raise ValueError("ARTIFACT_CONFLICT") from error
        if existing != content:
            raise ValueError("ARTIFACT_CONFLICT")
        return
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _invalid(artifact_id: str, reason_code: str) -> dict[str, Any]:
    return {"artifact_id": artifact_id, "valid": False, "reason_code": reason_code}


def _validate_component(value: str) -> None:
    if (
        not isinstance(value, str)
        or not _COMPONENT.fullmatch(value)
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("ARTIFACT_COMPONENT_INVALID")


def _validate_kind_content(kind: str, content: bytes) -> None:
    """Check a generically archived artifact against the schema for its kind.

    Raises before anything is written, so a rejected artifact leaves no file, no index
    row, and no id a later step could cite. `SCHEMA_INVALID` is the reason code the
    phase path already uses for the same failure, and unparseable bytes under a
    schema-bearing kind are that same failure: the kind is a promise that this is JSON
    of a known shape.
    """
    schema = _KIND_SCHEMAS.get(kind)
    if schema is None:
        return
    try:
        instance = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("SCHEMA_INVALID") from None
    if validate_named_schema(instance, schema):
        raise ValueError("SCHEMA_INVALID")


def _require_confined(path: Path, root: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("ARTIFACT_PATH_ESCAPE")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("ARTIFACT_PATH_ESCAPE") from error
    return resolved


def _matches(row: sqlite3.Row, expected: dict[str, str]) -> bool:
    return all(row[key] == value for key, value in expected.items())


def _descriptor(
    artifact_id: str,
    run_id: str,
    kind: str,
    content_hash: str,
    content_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "run_id": run_id,
        "kind": kind,
        "sha256": content_hash,
        "path": str(content_path),
        "metadata": metadata,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _envelope_hash(envelope: dict[str, Any]) -> str:
    canonical = _canonical_json(envelope.get("content"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_final_envelope(envelope: Any) -> None:
    if not isinstance(envelope, dict) or set(envelope) != _FINAL_ENVELOPE_KEYS:
        raise ValueError("ARTIFACT_ENVELOPE_INVALID")
    ensure_persistable(envelope)
    action_id, run_id, phase = (envelope.get(key) for key in ("action_id", "run_id", "phase"))
    if not all(isinstance(value, str) and value for value in (action_id, run_id, phase)):
        raise ValueError("ARTIFACT_ENVELOPE_INVALID")
    if phase not in _PHASE_SCHEMAS or envelope.get("host") != "comate" or envelope.get("schema_version") != "1":
        raise ValueError("ARTIFACT_ENVELOPE_INVALID")
    task_id = envelope.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id):
        raise ValueError("ARTIFACT_ENVELOPE_INVALID")
    if envelope.get("content_hash") != _envelope_hash(envelope):
        raise ValueError("CONTENT_HASH_MISMATCH")
    issues = validate_named_schema(envelope.get("content"), _PHASE_SCHEMAS[phase])
    if issues:
        raise ValueError("SCHEMA_INVALID")
    try:
        references = validate_evidence_refs(envelope.get("evidence_refs"))
    except ValueError as error:
        raise ValueError(str(error)) from None
    if not references:
        raise ValueError("EVIDENCE_REQUIRED")
    doc_id = envelope.get("knowledge_doc_id")
    url = envelope.get("knowledge_url")
    version = envelope.get("knowledge_version")
    comment_id = envelope.get("icafe_comment_id")
    if not all(isinstance(value, str) and value for value in (doc_id, url, version, comment_id)):
        raise ValueError("KNOWLEDGE_RECEIPT_INVALID")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "ku.baidu-int.com" or not parsed.path.rstrip("/").endswith(f"/{doc_id}") or parsed.query or parsed.fragment:
        raise ValueError("KNOWLEDGE_RECEIPT_INVALID")
