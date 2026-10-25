from __future__ import annotations

import re
from typing import Any


_SECRET_KEY = re.compile(
    r"(?:api[\s_-]*key|authorization|credential|password|private[\s_-]*key|secret|token)",
    re.IGNORECASE,
)
# KU mints document ids that can start with an underscore or dash, so a
# reference component may lead with either; a leading dot is still refused.
_REFERENCE_COMPONENT = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
_EVIDENCE_REFERENCE = re.compile(
    rf"(?:{_REFERENCE_COMPONENT}|artifact:{_REFERENCE_COMPONENT}|"
    rf"ku:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT}|"
    rf"icafe:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT}|"
    rf"ipipe:{_REFERENCE_COMPONENT}/{_REFERENCE_COMPONENT})"
)


# Value-level credential shapes. A placeholder (`<IREPO-TOKEN>`, `<token>`) or an
# already redacted value (`[REDACTED]`) is not a credential, so redaction is idempotent
# and the redacted G8 bindings that approvals are hashed over stay byte-identical.
_PLACEHOLDER = r"(?!<[^>\s]*>|\[REDACTED\])"
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # irepo download header, any length: `--header "IREPO-TOKEN:<token>"`, also with
    # whitespace after the separator or written as `IREPO-TOKEN=<token>`.
    (re.compile(rf"(?i)(IREPO-TOKEN\s*[:=]\s*){_PLACEHOLDER}[^\s\"'&]+"), r"\1<IREPO-TOKEN>"),
    # OAuth-style query/form parameter.
    (re.compile(rf"(?i)(access_token=){_PLACEHOLDER}[^&\s'\"]+"), r"\1[REDACTED]"),
    # Authorization header values (with or without a scheme word).
    (re.compile(rf"(?i)(\bAuthorization\b[\"']?\s*[:=]\s*[\"']?)(?:(?:Bearer|Basic|Token)[- ])?"
                rf"{_PLACEHOLDER}[A-Za-z0-9._~+/=-]{{8,}}"), r"\1[REDACTED]"),
    # Bearer credentials, including iPipe's `Bearer-<token>` spelling.
    (re.compile(rf"\bBearer[- ]{_PLACEHOLDER}[A-Za-z0-9._~+/=-]{{8,}}"), "Bearer [REDACTED]"),
)
# Exact secret values this process has loaded (irepo tokens from stage_parameters,
# the iPipe token from ipipe_client). Held in memory only and never printed; values
# shorter than this are too generic to replace by value.
_MIN_REGISTERED_SECRET = 6
_REGISTERED_SECRETS: set[str] = set()


def register_secret(value: Any) -> None:
    """Remember a credential value so it is redacted/rejected wherever it shows up."""
    if isinstance(value, str):
        text = value.strip()
        for candidate in {text, text.removeprefix("Bearer-"), text.removeprefix("Bearer ")}:
            if len(candidate) >= _MIN_REGISTERED_SECRET:
                _REGISTERED_SECRETS.add(candidate)


def redact_text(text: str) -> str:
    """`text` with every credential shape and every registered secret value replaced."""
    for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_secrets(value: Any) -> Any:
    """A copy of `value` with credential values redacted in every string (keys too)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            (redact_text(key) if isinstance(key, str) else key): redact_secrets(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(child) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(child) for child in value)
    return value


def _contains_secret_value(value: Any) -> bool:
    if isinstance(value, str):
        return redact_text(value) != value
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and _contains_secret_value(key)) or _contains_secret_value(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_value(child) for child in value)
    return False


def ensure_persistable(value: Any) -> None:
    """Refuse to persist secret-named fields or credential-shaped values.

    Writers that hold diagnostic text (error bodies, observations, approval material)
    call `redact_secrets` first; anything that still carries a credential is refused,
    the same fail-closed answer as a secret-named key.
    """
    if _contains_secret_key(value) or _contains_secret_value(value):
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
