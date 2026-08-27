from __future__ import annotations

import re
from typing import Any


_SECRET_KEY = re.compile(
    r"(?:api[\s_-]*key|authorization|credential|password|private[\s_-]*key|secret|token)",
    re.IGNORECASE,
)
_REFERENCE_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_EVIDENCE_REFERENCE = re.compile(
    rf"(?:{_REFERENCE_COMPONENT}|artifact:{_REFERENCE_COMPONENT}|"
    rf"ku:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT}|"
    rf"icafe:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT}|"
    rf"ipipe:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT})"
)


def ensure_persistable(value: Any) -> None:
    if _contains_secret_key(value):
        raise ValueError("PERSISTENCE_SECRET_REJECTED")


def validate_evidence_refs(value: Any) -> list[str]:
    ensure_persistable(value)
    if not isinstance(value, list):
        raise ValueError("EVIDENCE_REF_INVALID")

    references: list[str] = []
    for reference in value:
        if not isinstance(reference, str):
            raise ValueError("EVIDENCE_REF_INVALID")
        if _SECRET_KEY.search(reference):
            raise ValueError("PERSISTENCE_SECRET_REJECTED")
        if not _EVIDENCE_REFERENCE.fullmatch(reference):
            raise ValueError("EVIDENCE_REF_INVALID")
        references.append(reference)
    if len(references) != len(set(references)):
        raise ValueError("EVIDENCE_REF_INVALID")
    return references


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                return True
            if _contains_secret_key(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(child) for child in value)
    return False
