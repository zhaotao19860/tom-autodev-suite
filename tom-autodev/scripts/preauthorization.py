"""Standing approval for a test card, so an unattended run does not wait on a person.

A person grants it once, in `<config_root>/preauthorizations.json`, for one card of one
project, a fixed set of gates, the exact branches the card may touch, and an expiry.
Every gate the worker then asks for is still requested, delivered on both channels and
recorded in the ledger as usual; a matching grant only answers it with APPROVE on the
grantor's behalf, and leaves an audit record naming the grant. Nothing else changes: the
hash binding, the evidence checks and the gate order are the same as for a human click.

The file is local operator configuration, never part of the suite repository:

    {"grants": [{
        "grant_id": "bgw-2019-autotest",
        "card_id": "BGW-2019", "project": "bgw",
        "gates": ["G0", "G1", ..., "G9"],
        "branches": ["x86bgw-dpdk-2011-autodev", "pipline_case_autodev"],
        "responder": "someone@example.com",
        "expires_at": "2026-11-15T00:00:00+00:00"}]}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from approval_ledger import gate_of

FILE_NAME = "preauthorizations.json"


def _grants(config_root: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads((Path(config_root) / FILE_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    grants = value.get("grants") if isinstance(value, dict) else None
    return [item for item in grants or [] if isinstance(item, dict)]


def _expired(value: Any, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if expires.tzinfo is None:
        return True
    return now >= expires


def matching_grant(
    config_root: Path, *, card_id: Any, project: Any, gate: Any, profile: dict[str, Any],
    member_policy: dict[str, list[str]], now: datetime | None = None,
) -> dict[str, Any] | None:
    """The first unexpired grant covering this gate of this card, or None.

    Every branch the profile names must be one the grant lists, so a grant for test
    branches never answers a gate of a run that could reach a real branch. The
    responder must be a member the gate was delivered to on every channel.
    """
    now = now or datetime.now(timezone.utc)
    branches = {
        str(item.get("branch") or "")
        for item in [*(profile.get("business_repos") or []), profile.get("test_repo") or {}]
        if isinstance(item, dict)
    }
    for grant in _grants(config_root):
        if grant.get("card_id") != card_id or grant.get("project") != project:
            continue
        if gate_of(gate) not in set(grant.get("gates") or []):
            continue
        if _expired(grant.get("expires_at"), now):
            continue
        allowed = set(grant.get("branches") or [])
        if not branches or "" in branches or not branches <= allowed:
            continue
        responder = grant.get("responder")
        if not isinstance(responder, str) or not responder or not all(
            responder in (members or []) for members in member_policy.values()
        ):
            continue
        if not isinstance(grant.get("grant_id"), str) or not grant["grant_id"]:
            continue
        return grant
    return None
