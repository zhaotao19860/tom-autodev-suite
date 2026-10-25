"""Durable, control-plane-proven dependency evidence for one owned iPipe *stage*.

The diagnosis guard (`PhaseProtocol._authoritative_failure_class`) may only treat a
failure as an environment failure on *strong* evidence. Besides structured platform
fields (handled in `clients.ipipe_runtime._structured_environment_class`), the strong
evidence is a dependency the control plane itself failed to fetch *for the stage that
failed*. Every product-download outcome of a manual stage's parameters
(`get_bgwagent`, `get_bgw_test_case`) is appended, one key per attempt, by
`record_dependency_attempt` (called from `orchestrator._stage_parameters` only when the
caller is about to write G8 material or execute; read-only `--print-parameters` /
`--print-input-hash` never record):

    ipipe.dependency-failure:{run}:{build}:{stage_build_id}:{module}:{attempt:04d}

B-21 rules:

* bound to the stage: a record proves something only for the stage it names
  (`stage_build_id`, with `stage_conf_id` for audit) and only when that stage's own
  parameter declaration consumes the module (`consumed_by` non-empty and a subset of
  `declared_parameters`). Another stage of the same build failing to fetch a product
  says nothing about this stage's failure;
* only 403/404 on every exact candidate is authoritative (`ENV_UNSATISFIED`). Any 5xx
  or a candidate without a status code is kept as `advisory` only and never blocks;
* the latest attempt wins: each attempt gets its own append-only key (auditable), and
  `proven_dependency_failure` reads only the newest attempt per stage/module, so a later
  successful download (or a 5xx) supersedes an earlier 404;
* the `ipipe-rebuild` read-only probe (403/404 on the compile product) only proves the
  old build cannot be re-run from its products. It stays in the G8 material and is never
  written here.

Keyword matches in stage/job names or messages are never strong evidence.
"""
from __future__ import annotations

from typing import Any

GONE_STATUSES = frozenset({403, 404})
_PREFIX = "ipipe.dependency-failure"
_MAX_APPEND_RETRIES = 5


def stage_prefix(run_id: str, build_id: Any, stage_build_id: Any) -> str:
    return f"{_PREFIX}:{run_id}:{build_id}:{stage_build_id}:"


def dependency_key(run_id: str, build_id: Any, stage_build_id: Any, module: Any, attempt: int) -> str:
    return f"{stage_prefix(run_id, build_id, stage_build_id)}{module}:{attempt:04d}"


def _status_code(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def class_for_statuses(statuses: Any) -> str | None:
    """`ENV_UNSATISFIED` only when every candidate answered 403/404; otherwise None.

    A 5xx may come back and a missing status proves nothing, so neither is
    authoritative (see `advisory_for_statuses`).
    """
    codes = [_status_code(code) for code in statuses or []]
    if codes and all(code in GONE_STATUSES for code in codes):
        return "ENV_UNSATISFIED"
    return None


def advisory_for_statuses(statuses: Any) -> str | None:
    """A diagnosis hint for a non-authoritative download failure; never blocks a route."""
    codes = [_status_code(code) for code in statuses or []]
    if not codes or class_for_statuses(codes) is not None:
        return None
    if any(code is not None and 500 <= code <= 599 for code in codes):
        return "ENV_TRANSIENT_SUSPECTED"
    return "DOWNLOAD_STATUS_UNPROVEN"


def _candidate_statuses(result: dict[str, Any]) -> list[Any]:
    """One status per exact candidate the download check tried (None when it had none)."""
    candidates = result.get("candidates")
    if isinstance(candidates, list) and candidates:
        return [item.get("http_status") if isinstance(item, dict) else None for item in candidates]
    check = result.get("download_check")
    if isinstance(check, dict):
        return [check.get("http_status")]
    return []


def _sortable(statuses: list[Any]) -> list[Any]:
    codes = sorted({code for code in statuses if _status_code(code) is not None})
    return codes + ([None] if any(_status_code(code) is None for code in statuses) else [])


def _append(state: Any, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Store `record` under the next attempt number for its stage/module (append-only)."""
    prefix = stage_prefix(run_id, record["build_id"], record["stage_build_id"]) + f"{record['module']}:"
    for _ in range(_MAX_APPEND_RETRIES):
        attempt = len(state.idempotency_results_with_prefix(prefix)) + 1
        stored = {**record, "attempt": attempt}
        try:
            state.save_idempotency_result(
                dependency_key(run_id, record["build_id"], record["stage_build_id"],
                               record["module"], attempt), stored)
        except ValueError:
            continue  # a concurrent writer took this attempt number; recount
        return stored
    raise ValueError("DEPENDENCY_EVIDENCE_APPEND_CONFLICT")


def record_dependency_attempt(
    state: Any, run_id: str, *, build_id: Any, stage_build_id: Any, stage_conf_id: Any,
    module: Any, consumed_by: Any, declared_parameters: Any, result: Any, source: str,
) -> dict[str, Any] | None:
    """Append one product-download outcome for `module` as consumed by one stage.

    Recorded outcomes: `AVAILABLE` (the download verified; supersedes an earlier 404)
    and `DOWNLOAD_UNVERIFIED` (`PRODUCT_DOWNLOAD_UNVERIFIED`), the latter authoritative
    only when every candidate answered 403/404. Anything else is not a download outcome
    and is not recorded (None).
    """
    if not isinstance(result, dict) or not build_id or not stage_build_id or not module:
        return None
    if result.get("ok"):
        outcome, statuses = "AVAILABLE", []
    elif result.get("reason_code") == "PRODUCT_DOWNLOAD_UNVERIFIED":
        outcome, statuses = "DOWNLOAD_UNVERIFIED", _candidate_statuses(result)
    else:
        return None
    return _append(state, run_id, {
        "run_id": run_id, "build_id": str(build_id), "stage_build_id": str(stage_build_id),
        "stage_conf_id": str(stage_conf_id or ""), "module": str(module),
        "consumed_by": sorted(str(name) for name in consumed_by or []),
        "declared_parameters": sorted(str(name) for name in declared_parameters or []),
        "outcome": outcome,
        "classification": class_for_statuses(statuses) if outcome != "AVAILABLE" else None,
        "advisory": advisory_for_statuses(statuses) if outcome != "AVAILABLE" else None,
        "http_statuses": _sortable(statuses), "source": source,
    })


def _consumes(record: dict[str, Any]) -> bool:
    consumed = record.get("consumed_by")
    declared = record.get("declared_parameters")
    return (isinstance(consumed, list) and bool(consumed) and isinstance(declared, list)
            and set(consumed) <= set(declared))


def latest_attempts(state: Any, run_id: str, build_id: Any, stage_build_id: Any) -> list[dict[str, Any]]:
    """The newest recorded attempt per module for one stage of one build."""
    build, stage = str(build_id or ""), str(stage_build_id or "")
    if not build or not stage:
        return []
    newest: dict[str, dict[str, Any]] = {}
    for _key, stored in state.idempotency_results_with_prefix(stage_prefix(run_id, build, stage)):
        if (not isinstance(stored, dict) or stored.get("build_id") != build
                or stored.get("stage_build_id") != stage or not isinstance(stored.get("attempt"), int)):
            continue
        module = str(stored.get("module") or "")
        if module not in newest or stored["attempt"] > newest[module]["attempt"]:
            newest[module] = stored
    return [newest[module] for module in sorted(newest)]


def proven_dependency_failure(
    state: Any, run_id: str, build_id: Any, stage_build_id: Any,
) -> dict[str, Any] | None:
    """The authoritative dependency failure of exactly this stage, or None.

    Per module, the newest decisive attempt counts: a proven `ENV_UNSATISFIED` (all
    403/404) or a successful download (`AVAILABLE`). A later inconclusive attempt (5xx,
    missing status) proves nothing either way, so it neither creates nor clears the
    block. The stage's own declaration must consume the module.
    """
    build, stage = str(build_id or ""), str(stage_build_id or "")
    if not build or not stage:
        return None
    decisive: dict[str, dict[str, Any]] = {}
    for _key, stored in state.idempotency_results_with_prefix(stage_prefix(run_id, build, stage)):
        if (not isinstance(stored, dict) or stored.get("build_id") != build
                or stored.get("stage_build_id") != stage or not isinstance(stored.get("attempt"), int)):
            continue
        if stored.get("outcome") != "AVAILABLE" and stored.get("classification") != "ENV_UNSATISFIED":
            continue
        module = str(stored.get("module") or "")
        if module not in decisive or stored["attempt"] > decisive[module]["attempt"]:
            decisive[module] = stored
    for module in sorted(decisive):
        record = decisive[module]
        if record.get("classification") == "ENV_UNSATISFIED" and _consumes(record):
            return record
    return None
