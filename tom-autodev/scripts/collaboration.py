from __future__ import annotations

from execution_guard import guard_execution

import hashlib
import json
import re
from typing import Any

from approval_ledger import ApprovalLedger
from state_store import StateStore


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MENTION = re.compile(r"@([^\s,;:()\[\]{}<>]+@[^\s,;:()\[\]{}<>]+\.[^\s,;:()\[\]{}<>]+)")
_ROUTE_ROLES = {
    "environment": ("test",), "test-data": ("test",), "test-case": ("test",),
    "regression": ("test",), "integration": ("test",), "code": ("development",),
    "interface": ("development",), "spec": ("development",), "task-plan": ("development",),
    "review": ("development",), "mixed": ("development", "test"),
}
_PREPARED_BINDING_KEYS = frozenset({
    "run_id", "project", "card_id", "card_title", "card_content_hash", "profile_hash",
    "group_name", "group_topic", "owner", "roles", "member_snapshot", "friendlyLevel",
    "session_idempotency_key",
})
_INTAKE_PREREQUISITE_KEYS = (
    "requirement_id", "project", "profile_hash", "requirement_snapshot",
    "collaboration_binding",
)
# v2 also binds the control policy the owner is authorizing at G0: the change class chosen
# for the run and the workflow_spec version hash (which covers class routing + knowledge
# scope). Changing either changes the G0 input hash, so an old APPROVE cannot be reused for
# a run that now runs a different class or under a re-edited policy. Legacy INTAKE payloads
# carry no `intake_hash_version`; they hash over the v1 keys exactly as before.
_INTAKE_PREREQUISITE_KEYS_V2 = _INTAKE_PREREQUISITE_KEYS + (
    "change_class", "workflow_spec_hash", "intake_hash_version",
)
_GROUP_TOPIC_MAX = 12
# Failures the group client raises before it dispatches anything: the request was
# rejected locally or the gateway was unreachable, so no group can exist yet.
_UNDISPATCHED = frozenset({
    "INFOFLOW_GATEWAY_UNAVAILABLE", "INFOFLOW_GATEWAY_MISSING",
    "INFOFLOW_GATEWAY_INSTALL_FAILED", "INFOFLOW_GATEWAY_JOURNAL_MISMATCH",
    "GROUP_REQUEST_INVALID", "GROUP_MEMBERS_INVALID", "GROUP_FRIENDLY_LEVEL_INVALID",
    "MEMBER_CONFIRMATION_REQUIRED",
})


def _reason(error: BaseException) -> str:
    return str(error) or error.__class__.__name__


def _not_dispatched(error: BaseException) -> bool:
    return _reason(error) in _UNDISPATCHED


def resolve_members(members: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(members, dict):
        return {"reason_code": "MEMBER_CONFIRMATION_REQUIRED", "unresolved": []}
    fixed = {role: _emails(members.get(role)) for role in ("development", "test", "project")}
    unresolved = [value for values in fixed.values() for value in values[1]]
    if any(not fixed[role][0] for role in fixed):
        return {"reason_code": "MEMBER_CONFIRMATION_REQUIRED", "unresolved": sorted(set(unresolved))}
    roles = {role: list(fixed[role][0]) for role in fixed}
    icafe = members.get("icafe_responsible")
    if isinstance(icafe, dict):
        for role in roles:
            values, bad = _emails(icafe.get(role))
            roles[role].extend(values)
            unresolved.extend(bad)
    if members.get("allow_card_owners") is True:
        values, bad = _emails(members.get("card_owners"))
        roles["project"].extend(values)
        unresolved.extend(bad)
    if unresolved:
        return {"reason_code": "MEMBER_CONFIRMATION_REQUIRED", "unresolved": sorted(set(unresolved))}
    roles = {role: sorted(set(values)) for role, values in roles.items()}
    return {
        "reason_code": "OK", "roles": roles,
        "owner": fixed["project"][0][0],
        "member_snapshot": sorted({email for values in roles.values() for email in values}),
    }


def build_collaboration_binding(
    run_id: str,
    project: str,
    card_id: str,
    profile_hash: str,
    title: str,
    card_content_hash: str,
    members: dict[str, Any],
    group_topic: Any = None,
) -> dict[str, Any]:
    resolved = resolve_members(members)
    if resolved["reason_code"] != "OK":
        return resolved
    topic = _group_topic(title, group_topic)
    return {
        "reason_code": "OK",
        "binding": {
            "run_id": run_id,
            "project": project,
            "card_id": card_id,
            "card_title": title,
            "card_content_hash": card_content_hash,
            "profile_hash": profile_hash,
            "group_topic": topic,
            "group_name": _group_name(card_id, topic),
            "owner": resolved["owner"],
            "roles": resolved["roles"],
            "member_snapshot": resolved["member_snapshot"],
            "friendlyLevel": 3,
            "session_idempotency_key": f"infoflow.group.create:{run_id}",
        },
    }


def group_id_for_run(state_store: Any, run_id: Any) -> str | None:
    """The run's collaboration group, when G0 already created one.

    Approvals and progress belong in that group rather than in private chats: it is
    where the requirement's people already are, so a decision has the same audience
    as the work it gates. Returning None keeps the single-chat path available for a
    run whose group does not exist yet.
    """
    if not isinstance(run_id, str) or not run_id:
        return None
    found = state_store.result_by_idempotency_key(f"infoflow.group.create:{run_id}")
    if not isinstance(found, dict):
        return None
    receipt = found.get("receipt")
    response = receipt.get("response") if isinstance(receipt, dict) else None
    group_id = response.get("group_id") if isinstance(response, dict) else None
    return group_id if isinstance(group_id, str) and group_id.isdigit() else None


def intake_prerequisites(payload: Any) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    keys = (
        _INTAKE_PREREQUISITE_KEYS_V2
        if source.get("intake_hash_version") == "v2"
        else _INTAKE_PREREQUISITE_KEYS
    )
    return {key: source.get(key) for key in keys}


def intake_input_hash(payload: Any) -> str:
    return _hash(intake_prerequisites(payload))


def intake_prerequisites_valid(payload: Any, run_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    snapshot = payload.get("requirement_snapshot")
    binding = payload.get("collaboration_binding")
    roles = binding.get("roles") if isinstance(binding, dict) else None
    if (
        not isinstance(snapshot, dict)
        or not isinstance(binding, dict)
        or set(binding) != _PREPARED_BINDING_KEYS
        or binding.get("run_id") != run_id
        or binding.get("project") != payload.get("project")
        or binding.get("card_id") != payload.get("requirement_id")
        or binding.get("card_id") != snapshot.get("canonical_card_id")
        or binding.get("card_title") != snapshot.get("title")
        or binding.get("card_content_hash") != snapshot.get("content_hash")
        or binding.get("profile_hash") != payload.get("profile_hash")
        or binding.get("friendlyLevel") != 3
        or binding.get("session_idempotency_key") != f"infoflow.group.create:{run_id}"
        or not _valid_group_topic(binding.get("group_topic"))
        or binding.get("group_name") != _group_name(
            str(binding.get("card_id", "")), str(binding.get("group_topic", ""))
        )
    ):
        return False
    resolved = resolve_members(roles)
    if resolved.get("reason_code") != "OK":
        return False
    if (
        resolved.get("roles") != roles
        or binding.get("owner") != resolved.get("owner")
        or binding.get("member_snapshot") != resolved.get("member_snapshot")
    ):
        return False
    return payload.get("g0_input_hash") == intake_input_hash(payload)


class CollaborationSession:
    def __init__(self, state_store: StateStore, group_client: Any, *, approvals: ApprovalLedger | None = None):
        self.state = state_store
        self.group_client = group_client
        self.approvals = approvals
        self._sessions: dict[str, dict[str, Any]] = {}

    def resolve_members(self, members: dict[str, Any]) -> dict[str, Any]:
        return resolve_members(members)

    def prepare_g0(self, run_id: str, project: str, card: dict[str, Any], members: dict[str, Any]) -> dict[str, Any]:
        context = self._run_context(run_id)
        if self.approvals is not None:
            supplied_card_id = card.get("id") if isinstance(card, dict) else None
            if not isinstance(supplied_card_id, str) or not supplied_card_id:
                return {"run_id": run_id, "reason_code": "G0_BINDING_MISMATCH"}
            if context is None or context["project"] != project or context["card_id"] != supplied_card_id:
                return {"run_id": run_id, "reason_code": "G0_BINDING_MISMATCH"}
            prepared = context.get("prepared_request")
            if isinstance(prepared, dict):
                return {
                    "reason_code": "OK",
                    "request": dict(prepared),
                    "input_hash": context["g0_input_hash"],
                }
            resolved = self.resolve_members(context["role_members"])
            card_id, title = context["card_id"], context["card_title"]
        else:
            supplied_card_id, supplied_title = _card(card)
            resolved = self.resolve_members(members)
            if context is None:
                context = {"project": project, "card_id": supplied_card_id, "profile_hash": "legacy"}
            card_id, title = supplied_card_id, supplied_title
        if resolved["reason_code"] != "OK":
            return {"run_id": run_id, **resolved}
        if context["project"] != project or context["card_id"] != card_id:
            return {"run_id": run_id, "reason_code": "G0_BINDING_MISMATCH"}
        name = _group_name(card_id, _group_topic(title, context.get("group_topic")))
        binding = {
            "run_id": run_id, "project": project, "card_id": card_id,
            "profile_hash": context["profile_hash"], "group_name": name,
            "owner": resolved["owner"], "member_snapshot": resolved["member_snapshot"],
        }
        if self.approvals is not None and isinstance(context.get("card_content_hash"), str):
            binding["card_content_hash"] = context["card_content_hash"]
        request = {**binding, "roles": resolved["roles"], "friendlyLevel": 3}
        return {"reason_code": "OK", "request": request, "input_hash": _hash(request)}

    @guard_execution
    def create(
        self, run_id: str, project: str, card: dict[str, Any], members: dict[str, Any], *,
        approval_id: str | None = None, input_hash: str | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_g0(run_id, project, card, members)
        if prepared["reason_code"] != "OK":
            return prepared
        if self.approvals is None:
            binding = members.get("g0_approval") if isinstance(members, dict) else None
            if not isinstance(binding, dict) or binding.get("action") != "G0" or binding.get("effective_decision") != "APPROVE" or binding.get("group_name") != prepared["request"]["group_name"] or binding.get("owner") != prepared["request"]["owner"] or binding.get("member_snapshot") != prepared["request"]["member_snapshot"]:
                return {"run_id": run_id, "reason_code": "G0_BINDING_REQUIRED"}
        elif not self._approved_g0(approval_id, run_id, prepared["input_hash"], input_hash):
            return {"run_id": run_id, "reason_code": "G0_BINDING_MISMATCH"}
        request = {**prepared["request"], "input_hash": prepared["input_hash"], "approval_id": approval_id}
        key = f"infoflow.group.create:{run_id}"
        claim = self.state.claim_intent(run_id, "infoflow.group.create", key, request)
        if claim["status"] == "CONFLICT":
            return {"run_id": run_id, "reason_code": "GROUP_CONFLICT"}
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return self._restore(completed["receipt"]["response"])
        intent = claim["intent"]
        if claim["status"] == "EXISTING":
            reconciled = self.group_client.reconcile_group(request)
            if reconciled is None:
                return {"run_id": run_id, "reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
            return self._save_group(intent, request, reconciled)
        try:
            response = self.group_client.create_or_reuse(request)
        except Exception as error:
            if _not_dispatched(error):
                self.state.withdraw_intent(run_id, "infoflow.group.create", key, request)
                return {
                    "run_id": run_id, "reason_code": _reason(error), "retry_allowed": True,
                }
            reconciled = self.group_client.reconcile_group(request)
            if reconciled is not None:
                return self._save_group(intent, request, reconciled)
            return {"run_id": run_id, "reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
        return self._save_group(intent, request, response)

    def route_failure(self, failure_bundle: dict[str, Any]) -> dict[str, Any]:
        run_id = failure_bundle.get("run_id")
        session = self._session(run_id) if isinstance(run_id, str) else None
        if session is None:
            return {"reason_code": "COLLABORATION_SESSION_REQUIRED"}
        category = str(failure_bundle.get("category", "")).lower()
        if category in {"auth", "platform", "release-rule"}:
            recipients = [session["owner"]]
        else:
            roles = _ROUTE_ROLES.get(category, ("development", "test"))
            recipients = sorted({email for role in roles for email in session["roles"][role]})
        content = _failure_markdown(failure_bundle, recipients)
        return self.send_message(run_id, content, recipients, _hash({"content": content, "at_users": recipients}))

    @guard_execution
    def send_message(self, run_id: str, content: str, at_users: list[str], idempotency_key: str) -> dict[str, Any]:
        session = self._session(run_id)
        if session is None:
            return {"reason_code": "COLLABORATION_SESSION_REQUIRED"}
        recipients = sorted(set(at_users)) if isinstance(at_users, list) else []
        if not content or not idempotency_key or not all(_EMAIL.fullmatch(item) for item in recipients):
            return {"reason_code": "MESSAGE_INVALID"}
        if set(_MENTION.findall(content)) != set(recipients):
            return {"reason_code": "MESSAGE_MENTION_MISMATCH"}
        request = {"group_id": session["group_id"], "content": content, "at_users": recipients}
        key = f"infoflow.group.message:{run_id}:{idempotency_key}"
        claim = self.state.claim_intent(run_id, "infoflow.group.message", key, request)
        if claim["status"] == "CONFLICT":
            return {"reason_code": "MESSAGE_CONFLICT"}
        completed = self.state.result_by_idempotency_key(key)
        if completed is not None:
            return completed["receipt"]["response"]
        intent = claim["intent"]
        if claim["status"] == "EXISTING":
            reconciled = self.group_client.reconcile_message(session["group_id"], idempotency_key)
            if reconciled is None:
                return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
            return self._save_message(intent, request, reconciled)
        try:
            response = self.group_client.send_markdown(session["group_id"], content, recipients, idempotency_key)
        except Exception:
            reconciled = self.group_client.reconcile_message(session["group_id"], idempotency_key)
            if reconciled is not None:
                return self._save_message(intent, request, reconciled)
            return {"reason_code": "QUERY_REQUIRED", "intent_id": intent["intent_id"], "retry_allowed": False}
        return self._save_message(intent, request, response)

    def _run_context(self, run_id: str) -> dict[str, Any] | None:
        events = self.state.events(run_id)
        if not events or not isinstance(events[0].get("payload"), dict):
            return None
        payload = events[0]["payload"]
        project, card_id, profile_hash = payload.get("project"), payload.get("requirement_id"), payload.get("profile_hash")
        if not all(isinstance(value, str) and value for value in (project, card_id, profile_hash)):
            return None
        if self.approvals is None:
            return {"project": project, "card_id": card_id, "profile_hash": profile_hash}
        binding = payload.get("collaboration_binding")
        if not isinstance(binding, dict):
            return None
        if intake_prerequisites_valid(payload, run_id):
            request = {
                key: binding[key]
                for key in (
                    "run_id", "project", "card_id", "profile_hash", "group_name", "owner",
                    "member_snapshot", "roles", "friendlyLevel", "card_content_hash",
                )
            }
            return {
                "project": project,
                "card_id": card_id,
                "prepared_request": request,
                "g0_input_hash": payload["g0_input_hash"],
            }
        bound_card = binding.get("card")
        role_members = binding.get("role_members")
        if (
            not isinstance(bound_card, dict)
            or bound_card.get("id") != card_id
            or not isinstance(bound_card.get("title"), str)
            or not bound_card["title"]
            or not isinstance(bound_card.get("content_hash"), str)
            or len(bound_card["content_hash"]) != 64
            or not isinstance(role_members, dict)
        ):
            return None
        return {
            "project": project,
            "card_id": card_id,
            "card_title": bound_card["title"],
            "card_content_hash": bound_card.get("content_hash"),
            "profile_hash": profile_hash,
            "role_members": role_members,
            "group_topic": binding.get("group_topic"),
        }

    def _approved_g0(self, approval_id: Any, run_id: str, canonical_hash: str, supplied_hash: Any) -> bool:
        if not isinstance(approval_id, str) or supplied_hash != canonical_hash or self.approvals is None:
            return False
        approval = self.approvals.get(approval_id)
        return bool(approval and approval.get("run_id") == run_id and approval.get("action") == "G0" and approval.get("input_hash") == canonical_hash and approval.get("effective_decision") == "APPROVE")

    def _session(self, run_id: str) -> dict[str, Any] | None:
        cached = self._sessions.get(run_id)
        if cached is not None:
            return cached
        found = self.state.result_by_idempotency_key(f"infoflow.group.create:{run_id}")
        return self._restore(found["receipt"]["response"]) if found is not None else None

    def _restore(self, result: dict[str, Any]) -> dict[str, Any]:
        self._sessions[result["run_id"]] = result
        return result

    def _save_group(self, intent: dict[str, Any], request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        group_id = response.get("group_id") if isinstance(response, dict) else None
        if not isinstance(group_id, (str, int)) or not str(group_id):
            return {"reason_code": "GROUP_CREATE_RESPONSE_INVALID"}
        result = {"run_id": intent["run_id"], "group_id": str(group_id), "group_name": request["group_name"], "owner": request["owner"], "roles": request["roles"], "member_snapshot": request["member_snapshot"], "bot_id": str(response["bot_id"]) if response.get("bot_id") is not None else None, "message_receipts": [], "approval_requests": [request["input_hash"]], "g0_approval_id": request.get("approval_id")}
        self.state.receipt(intent["intent_id"], result, [])
        return self._restore(result)

    def _save_message(self, intent: dict[str, Any], request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict) or not response.get("message_id"):
            return {"reason_code": "MESSAGE_RESPONSE_INVALID"}
        result = {"run_id": intent["run_id"], "group_id": request["group_id"], "message_id": str(response["message_id"]), "content": request["content"], "at_users": request["at_users"]}
        self.state.receipt(intent["intent_id"], result, [])
        return result


def _emails(values: Any) -> tuple[list[str], list[str]]:
    valid, invalid = [], []
    if not isinstance(values, list):
        return valid, invalid
    for value in values:
        email = value.get("email") if isinstance(value, dict) else value
        if isinstance(email, str) and _EMAIL.fullmatch(email): valid.append(email)
        elif isinstance(email, str) and email: invalid.append(email)
    return valid, invalid

def _card(card: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(card, dict) or not isinstance(card.get("id"), str) or not isinstance(card.get("title"), str) or not card["id"] or not card["title"]:
        raise ValueError("COLLABORATION_CARD_INVALID")
    return card["id"], card["title"]

def _short_title(title: str) -> str: return re.sub(r"\s+", "-", title.strip())


def _group_name(card_id: str, topic: str) -> str:
    return f"{card_id}-{topic}" if topic else card_id


def _valid_group_topic(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _GROUP_TOPIC_MAX


def _group_topic(title: str, override: Any = None) -> str:
    """Short, human-readable topic for the collaboration group name.

    Infoflow group names are read in a sidebar, so an untruncated card title is
    unusable. A profile may pin an explicit topic; otherwise the card title is
    stripped of a leading bracketed tag and trailing punctuation, then bounded.
    """
    if isinstance(override, str) and override.strip():
        return override.strip()[:_GROUP_TOPIC_MAX]
    cleaned = re.sub(r"^\s*[\[【(（][^\]】)）]*[\]】)）]\s*", "", str(title).strip())
    cleaned = _short_title(cleaned).strip("-：:；;，,。.")
    return (cleaned or _short_title(str(title)))[:_GROUP_TOPIC_MAX]
def _hash(value: dict[str, Any]) -> str: return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def _safe(value: Any) -> str: return str(value).replace("@", "[at]")[:800]
def _failure_markdown(failure: dict[str, Any], recipients: list[str]) -> str:
    evidence = failure.get("evidence") if isinstance(failure.get("evidence"), dict) else {}
    lines = ["## Failure routing", " ".join(f"@{email}" for email in recipients), "", "### Summary", _safe(failure.get("summary", ""))]
    for label, key in (("iCafe", "icafe"), ("KU", "ku"), ("Revision", "revision"), ("Pipeline", "pipeline"), ("Build", "build"), ("Stage", "stage"), ("Job", "job")):
        if isinstance(evidence.get(key), str) and evidence[key]: lines.append(f"- {label}: {_safe(evidence[key])}")
    if failure.get("next_action"): lines.extend(["", "### Next action", _safe(failure["next_action"])[:300]])
    return "\n".join(lines)
