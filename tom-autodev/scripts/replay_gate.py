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
    """Compare the draft-cache entries for one input across the models that produced them,
    grouped by prompt identity (R-M3).

    Models are only comparable within the SAME prompt version: otherwise a different prompt's
    output for model A could stand in for A in another prompt's group and mask a real
    divergence. Each prompt group needs at least two *known* model identities to conclude
    (with fewer — e.g. the agent-turn backend records model=None — it is INSUFFICIENT_EVIDENCE
    rather than a false CONSISTENT); a group divergence is a hard signal a cross-model swap
    changed behavior. The overall status is DIVERGENT if any group diverges, else CONSISTENT
    if any group could be compared, else INSUFFICIENT_EVIDENCE. Byte-identical output only
    proves integrity, not quality parity. Advisory: it reports, it does not itself block.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(str(entry.get("prompt_version") or ""), []).append(entry)
    group_reports: dict[str, dict[str, Any]] = {}
    for prompt, group in groups.items():
        known_models = sorted({e.get("model") for e in group if e.get("model")})
        distinct = sorted({e["output_hash"] for e in group})
        if len(known_models) < 2:
            status = "INSUFFICIENT_EVIDENCE"
        elif len(distinct) <= 1:
            status = "CONSISTENT"
        else:
            status = "DIVERGENT"
        group_reports[prompt] = {
            "known_models": known_models,
            "models": {(e.get("model") or "agent-turn"): e["output_hash"] for e in group},
            "distinct_outputs": len(distinct),
            "status": status,
        }
    statuses = {report["status"] for report in group_reports.values()}
    if "DIVERGENT" in statuses:
        overall = "DIVERGENT"
    elif "CONSISTENT" in statuses:
        overall = "CONSISTENT"
    else:
        overall = "INSUFFICIENT_EVIDENCE"
    return {
        "input_hashes": sorted({entry["input_hash"] for entry in entries}),
        "groups": group_reports,
        "distinct_outputs": len(sorted({entry["output_hash"] for entry in entries})),
        "status": overall,
    }


def golden_replay(state: Any, run_id: str) -> dict[str, Any]:
    """Replay a run's ModelExecutionReceipts against the draft cache.

    For every producer fill the run recorded, confirm (1) a cached draft exists for its
    input_hash, (2) each cached draft still hashes to its stored output_hash (integrity),
    and (3) the receipt's own output_hash is backed by a cached draft of the SAME producer
    identity (prompt version + model), so a receipt cannot be vouched for by a cache entry
    from a different prompt/model that merely happens to share an output hash (R-M3). Returns
    ok=False with the offending inputs otherwise — the basis a golden-replay CI gate reads.
    Read-only.
    """
    receipts = state.model_execution_receipts(run_id)
    checked = 0
    mismatches: list[dict[str, Any]] = []
    missing_cache: list[str] = []
    for receipt in receipts:
        input_hash = receipt.get("input_hash")
        receipt_output = receipt.get("output_hash")
        entries = state.draft_cache_entries(input_hash) if input_hash else []
        if not entries:
            missing_cache.append(input_hash)
            continue
        backed = False
        for entry in entries:
            checked += 1
            if _output_hash(entry["draft"]) != entry["output_hash"]:
                mismatches.append({"input_hash": input_hash, "model": entry.get("model"),
                                   "reason": "cache_hash_drift"})
            if (
                entry["output_hash"] == receipt_output
                and entry.get("prompt_version") == receipt.get("prompt_version")
                and entry.get("model") == receipt.get("model")
            ):
                backed = True
        if not backed:
            mismatches.append({"input_hash": input_hash, "reason": "receipt_not_backed_by_cache",
                               "receipt_output": receipt_output})
    return {
        "ok": not mismatches and not missing_cache,
        "run_id": run_id,
        "receipts": len(receipts),
        "checked": checked,
        "mismatches": mismatches,
        "missing_cache": missing_cache,
    }
