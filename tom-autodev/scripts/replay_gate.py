from __future__ import annotations

import hashlib
import json
from typing import Any


def _output_hash(draft: Any) -> str:
    """The canonical output hash of a DraftContent, matching StateStore.cache_draft's own
    hashing, so a replay can recompute it from the stored draft and detect drift."""
    canonical = json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cross_model_diff(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the draft-cache entries for one input across the models that produced them.

    CONSISTENT when every model that produced this input agreed on the output (or only one
    model has produced it); DIVERGENT when two models produced different drafts for the same
    input — the signal a cross-model swap changed behavior. Advisory: it reports, it does
    not itself block.
    """
    by_model = {(entry.get("model") or "agent-turn"): entry["output_hash"] for entry in entries}
    distinct_outputs = sorted(set(by_model.values()))
    return {
        "input_hashes": sorted({entry["input_hash"] for entry in entries}),
        "models": by_model,
        "distinct_outputs": len(distinct_outputs),
        "status": "CONSISTENT" if len(distinct_outputs) <= 1 else "DIVERGENT",
    }


def golden_replay(state: Any, run_id: str) -> dict[str, Any]:
    """Replay a run's ModelExecutionReceipts against the draft cache.

    For every producer fill the run recorded, confirm a cached draft exists for its
    input_hash and that re-hashing the stored draft reproduces the stored output_hash
    (determinism / integrity). Returns ok=False with the offending inputs when a cached
    draft is missing or a stored draft no longer hashes to its recorded output — the basis
    a golden-replay CI gate reads. Read-only.
    """
    receipts = state.model_execution_receipts(run_id)
    checked = 0
    mismatches: list[dict[str, Any]] = []
    missing_cache: list[str] = []
    for receipt in receipts:
        input_hash = receipt.get("input_hash")
        entries = state.draft_cache_entries(input_hash) if input_hash else []
        if not entries:
            missing_cache.append(input_hash)
            continue
        for entry in entries:
            checked += 1
            if _output_hash(entry["draft"]) != entry["output_hash"]:
                mismatches.append({"input_hash": input_hash, "model": entry.get("model")})
    return {
        "ok": not mismatches and not missing_cache,
        "run_id": run_id,
        "receipts": len(receipts),
        "checked": checked,
        "mismatches": mismatches,
        "missing_cache": missing_cache,
    }
