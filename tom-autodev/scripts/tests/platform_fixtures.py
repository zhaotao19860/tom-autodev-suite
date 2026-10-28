"""Replay recorded platform responses (platform-fixture.v1) instead of hand-written dicts.

Hand-written fakes encoded what the controller's author believed iPipe returns; BGW-1995
showed five places where that belief was wrong. A fixture is what the gateway actually
returned, whitelisted and redacted by `fixtures/platform/record.py`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "platform"


def load(fixture_id: str) -> dict[str, Any]:
    record = json.loads((FIXTURES / f"{fixture_id}.json").read_text())
    body = json.dumps(record["response"], ensure_ascii=False, sort_keys=True)
    if hashlib.sha256(body.encode()).hexdigest() != record["content_sha256"]:
        raise AssertionError(f"fixture {fixture_id} was edited after recording")
    if record["origin"] == "DERIVED" and not record.get("derived_from"):
        raise AssertionError(f"derived fixture {fixture_id} does not name its source")
    return record


def response(fixture_id: str) -> Any:
    return copy.deepcopy(load(fixture_id)["response"])


def all_records() -> list[dict[str, Any]]:
    return [load(path.stem) for path in sorted(FIXTURES.glob("*.json"))]
