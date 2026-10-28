"""One state machine for every external write (MVP contract 3.1).

Each write is an intent in `external_intents`. Its state is derived from what is
already persisted -- the receipt, and `intent.state` observations -- so no new table
and no rewrite of history is needed:

    CLAIMED ──dispatch answered with identity──────────────▶ CONFIRMED
       │    └─failed locally, proven before any request────▶ ABANDONED_LOCAL
       └─no identity / timeout / error─▶ UNKNOWN
                                           ├─read-back finds exactly one candidate ▶ CONFIRMED
                                           ├─read-back proves it did not happen ───▶ CONFIRMED_ABSENT
                                           ├─read-back finds several candidates ──▶ AMBIGUOUS
                                           └─cannot tell ─────────────────────────▶ stays UNKNOWN

CONFIRMED, CONFIRMED_ABSENT, ABANDONED_LOCAL and ABANDONED (an operator's
`abandon-intent`) carry a receipt, so they no longer block the target. UNKNOWN and
AMBIGUOUS carry none, so `claim_exclusive_intent`'s prefix check keeps blocking every
other write to the same target: that check is this machine's CAS and stays the
authority. AMBIGUOUS is terminal for automation -- reconcile does not re-read it into
something else; a person decides.

Each operation brings its own read-back (`observe`), because "identity + started after
the intent" means different things for a CR, a build, a stage and a release. This module
only sequences them and records the verdicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

CLAIMED = "CLAIMED"
UNKNOWN = "UNKNOWN"
CONFIRMED = "CONFIRMED"
CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
AMBIGUOUS = "AMBIGUOUS"
ABANDONED_LOCAL = "ABANDONED_LOCAL"
ABANDONED = "ABANDONED"

# States that still hold the target: no other write to it may be claimed.
BLOCKING = frozenset({CLAIMED, UNKNOWN, AMBIGUOUS})
# States after which the same operation may be attempted again under a new intent.
RETRYABLE = frozenset({CONFIRMED_ABSENT, ABANDONED_LOCAL})

STATE_OBSERVATION = "intent.state"


@dataclass(frozen=True)
class Verdict:
    """What one read-back concluded about a claimed write."""

    state: str
    result: dict[str, Any] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def confirmed(result: dict[str, Any]) -> "Verdict":
        return Verdict(CONFIRMED, result)

    @staticmethod
    def unknown(result: dict[str, Any] | None = None, **evidence: Any) -> "Verdict":
        return Verdict(UNKNOWN, result, evidence)

    @staticmethod
    def ambiguous(**evidence: Any) -> "Verdict":
        return Verdict(AMBIGUOUS, None, evidence)

    @staticmethod
    def absent(**evidence: Any) -> "Verdict":
        return Verdict(CONFIRMED_ABSENT, None, evidence)


def intent_state(state_store: Any, intent: dict[str, Any]) -> str:
    """The state of a persisted intent, derived from its receipt and observations."""
    completed = state_store.result_by_idempotency_key(intent["idempotency_key"])
    if completed is not None:
        response = completed["receipt"]["response"]
        if isinstance(response, dict):
            if response.get("intent_state") == CONFIRMED_ABSENT:
                return CONFIRMED_ABSENT
            if response.get("abandoned") is True:
                return ABANDONED_LOCAL if response.get("local_error_proven") else ABANDONED
            if response.get("ok") is True:
                return CONFIRMED
        # A receipt with ok False and no state marker is a recorded platform refusal:
        # the write was answered, so the target is no longer held by it.
        return CONFIRMED
    recorded = [
        item.get("observation") or {}
        for item in state_store.external_observations(intent["run_id"], intent_id=intent["intent_id"])
    ]
    states = [o.get("state") for o in recorded if o.get("operation") == STATE_OBSERVATION]
    if AMBIGUOUS in states:
        return AMBIGUOUS
    if states:
        return states[-1]
    return CLAIMED


def sticky_ambiguous(state_store: Any, intent: dict[str, Any]) -> Verdict | None:
    """The recorded AMBIGUOUS verdict, with its evidence, if the intent already has one."""
    recorded = [
        item.get("observation") or {}
        for item in state_store.external_observations(intent["run_id"], intent_id=intent["intent_id"])
    ]
    found = [o for o in recorded
             if o.get("operation") == STATE_OBSERVATION and o.get("state") == AMBIGUOUS]
    if not found:
        return None
    evidence = {k: v for k, v in found[0].items() if k not in {"operation", "state"}}
    return Verdict(AMBIGUOUS, None, evidence)


def record_state(state_store: Any, intent_id: str, verdict: Verdict) -> None:
    """Append the verdict of a read-back as an observation (never a receipt)."""
    state_store.record_external_observation(intent_id, {
        "operation": STATE_OBSERVATION, "state": verdict.state, **verdict.evidence,
    })


def confirm(
    observe: Callable[[], Verdict],
    *,
    polls: int,
    interval: float,
    sleeper: Callable[[float], Any],
) -> Verdict:
    """Read back a write whose answer carried no identity, a bounded number of times.

    Stops at the first verdict that is not UNKNOWN. Never re-sends anything: `observe`
    is read-only by contract.
    """
    verdict = Verdict.unknown()
    for attempt in range(max(1, polls)):
        verdict = observe()
        if verdict.state != UNKNOWN:
            return verdict
        if attempt + 1 < polls:
            sleeper(interval)
    return verdict


def settle(
    state_store: Any,
    intent: dict[str, Any],
    verdict: Verdict,
) -> Verdict:
    """Persist a non-confirmed verdict; CONFIRMED is persisted by the operation's receipt.

    CONFIRMED_ABSENT is written as a receipt (it frees the target); UNKNOWN and AMBIGUOUS
    are observations, so the target stays held.
    """
    if verdict.state == CONFIRMED:
        return verdict
    if verdict.state == AMBIGUOUS and sticky_ambiguous(state_store, intent) is not None:
        return verdict  # recorded once; the first evidence stands
    if verdict.state == CONFIRMED_ABSENT:
        state_store.receipt(intent["intent_id"], {
            "ok": False, "reason_code": "INTENT_CONFIRMED_ABSENT",
            "intent_state": CONFIRMED_ABSENT, **verdict.evidence,
        }, [])
        return verdict
    record_state(state_store, intent["intent_id"], verdict)
    return verdict
