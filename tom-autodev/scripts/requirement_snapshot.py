from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, TypedDict


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_FIELDS = frozenset(
    {
        "canonical_card_id",
        "title",
        "body",
        "html",
        "acceptance",
        "fields",
        "attachments",
        "links",
        "status",
        "type",
        "responsible_people",
        "created",
        "modified",
        "content_hash",
    }
)


class RequirementSnapshot(TypedDict):
    canonical_card_id: str
    title: str
    body: str
    html: str
    acceptance: list[Any]
    fields: dict[str, Any]
    attachments: list[Any]
    links: list[Any]
    status: str
    type: str
    responsible_people: list[dict[str, Any]]
    created: dict[str, Any]
    modified: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class AcceptanceValueError(ValueError):
    index: int
    kind: str


def normalized_acceptance_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise AcceptanceValueError(-1, "invalid")
    identifiers: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        identifier = value if isinstance(value, str) else value.get("id") if isinstance(value, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            raise AcceptanceValueError(index, "invalid")
        identifier = identifier.strip()
        if identifier in seen:
            raise AcceptanceValueError(index, "duplicate")
        seen.add(identifier)
        identifiers.append(identifier)
    return identifiers


def content_hash(snapshot: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in snapshot.items() if key != "content_hash"}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validation_error(snapshot: Any, expected_card_id: str) -> str | None:
    if not isinstance(snapshot, dict) or not _REQUIRED_FIELDS.issubset(snapshot):
        return "ICAFE_SNAPSHOT_INVALID"
    if snapshot.get("canonical_card_id") != expected_card_id:
        return "ICAFE_SNAPSHOT_IDENTITY_MISMATCH"
    if not _valid_shape(snapshot):
        return "ICAFE_SNAPSHOT_INVALID"
    try:
        normalized_acceptance_ids(snapshot.get("acceptance"))
    except AcceptanceValueError:
        return "ICAFE_SNAPSHOT_INVALID"
    supplied_hash = snapshot["content_hash"]
    if not _SHA256.fullmatch(supplied_hash) or supplied_hash != content_hash(snapshot):
        return "ICAFE_SNAPSHOT_HASH_MISMATCH"
    return None


def _valid_shape(snapshot: dict[str, Any]) -> bool:
    if not all(
        isinstance(snapshot.get(field), str) and snapshot[field]
        for field in ("canonical_card_id", "title", "status", "type")
    ):
        return False
    if not all(isinstance(snapshot.get(field), str) for field in ("body", "html")):
        return False
    if not all(
        isinstance(snapshot.get(field), list)
        for field in ("acceptance", "attachments", "links", "responsible_people")
    ):
        return False
    if not isinstance(snapshot.get("fields"), dict):
        return False
    if not all(isinstance(person, dict) for person in snapshot["responsible_people"]):
        return False
    for field in ("created", "modified"):
        metadata = snapshot.get(field)
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("user"), dict)
            or not isinstance(metadata.get("time"), str)
        ):
            return False
    return isinstance(snapshot.get("content_hash"), str)
