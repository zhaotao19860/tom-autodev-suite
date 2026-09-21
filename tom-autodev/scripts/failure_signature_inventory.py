"""Read-only inventory for the failure-signature v2 boundary; never migrate by hash.

This command deliberately does not instantiate StateStore: its constructor may migrate
the database. A legacy aggregate has no original job messages, so it cannot safely be
split into v2 causes. All rows are retained, including resolved and non-iPipe signatures.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


def inventory(database: Path | str) -> dict[str, Any]:
    path = Path(database).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute("SELECT * FROM failure_cases ORDER BY signature").fetchall()
    finally:
        connection.close()
    counts = {"legacy_unversioned": 0, "v2": 0, "other": 0}
    cases = []
    for row in rows:
        signature = row["signature"]
        if re.fullmatch(r"ipipe-failure:v2:[0-9a-f]{64}", signature):
            version, status = "v2", "CURRENT_VERSION"
        elif re.fullmatch(r"[0-9a-f]{64}", signature):
            # An unprefixed digest does not prove an iPipe producer; raw evidence must.
            version, status = "legacy_unversioned", "RAW_OCCURRENCE_REPLAY_REQUIRED"
        else:
            version, status = "other", "PRODUCER_IDENTIFICATION_REQUIRED"
        counts[version] += 1
        case = dict(row)
        case["run_ids"] = json.loads(case.pop("run_ids_json"))
        case["resolved"] = bool(case["resolved"])
        cases.append({**case, "signature_version": version, "migration_status": status})
    return {
        "database": str(path), "read_only": True,
        "automatic_migration_safe": False, "counts": counts, "cases": cases,
        "replay_requirements": [
            "Retain original rows and frozen evidence; do not alias a legacy digest to v2.",
            "Recover original pipeline/module, failed stage identities and full failed-job messages for each occurrence.",
            "Recompute v2 per occurrence and preserve occurrence identity, resolution order and provenance before explicit backfill.",
            "Aggregate hashes, counts and truncated notification excerpts cannot prove a lossless migration.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Existing state.sqlite to inspect read-only")
    args = parser.parse_args()
    try:
        result = inventory(args.database)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"FAILURE_SIGNATURE_INVENTORY_UNAVAILABLE: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
