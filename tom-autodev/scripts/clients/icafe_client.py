from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from cli_transport import CliTransport, CliTransportError
from requirement_snapshot import content_hash as requirement_snapshot_hash
from state_store import StateStore


_CARD_ID = re.compile(r"^(.+)-([0-9]+)$")
_COMMENT_PREFIX = "tom-autodev-comment"
_KU_PATH = re.compile(
    r"^/knowledge/([A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)$"
)


class CafeClient:
    def __init__(
        self,
        fetcher: Callable[[str], dict[str, Any]] | None = None,
        *,
        transport: Any | None = None,
        state_store: StateStore | None = None,
        run_id: str | None = None,
        binary: str = "icafe-cli",
        preflight: bool = True,
        terminal_statuses: Iterable[str] = (),
        approval_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    ):
        self.fetcher = fetcher
        self.transport = transport or (None if fetcher is not None else CliTransport())
        self.state = state_store
        self.run_id = run_id
        self.binary = binary
        self.preflight = preflight
        self.terminal_statuses = frozenset(terminal_statuses)
        self.approval_lookup = approval_lookup
        self._preflight_complete = False

    def snapshot(self, card_id: str) -> dict[str, Any]:
        if self.fetcher is not None:
            snapshot = dict(self.fetcher(card_id))
            snapshot["content_hash"] = requirement_snapshot_hash(snapshot)
            return snapshot
        if card_id == "":
            found = self._invoke(
                [self.binary, "card", "smart-find"],
                "status",
            )
            if not found["ok"]:
                return found
            payload = found["payload"]
            candidates = payload.get("cards")
            if candidates is None and isinstance(payload.get("result"), dict):
                candidates = payload["result"].get("cards")
            if not isinstance(candidates, list) or not candidates:
                return _failure("ICAFE_CARD_NOT_FOUND")
            if len(candidates) != 1 or not isinstance(candidates[0], dict):
                return _failure("ICAFE_CARD_SELECTION_REQUIRED")
            candidate = candidates[0]
            space = candidate.get("spacePrefixCode")
            sequence = candidate.get("sequence")
            if not isinstance(space, str) or not space or not isinstance(sequence, (int, str)):
                return _failure("ICAFE_SMART_FIND_INVALID")
            card_id = f"{space}-{sequence}"
        parsed = self._parse_card_id(card_id)
        if parsed is None:
            return _failure("ICAFE_CARD_ID_INVALID")
        space, sequence = parsed
        response = self._invoke(
            [
                self.binary,
                "card",
                "get",
                "--space",
                space,
                "--sequence",
                sequence,
                "--brief",
            ],
            "code",
        )
        if not response["ok"]:
            return response
        cards = response["payload"].get("cards")
        if not isinstance(cards, list) or len(cards) != 1 or not isinstance(cards[0], dict):
            return _failure("ICAFE_SNAPSHOT_INVALID")
        card = cards[0]
        if str(card.get("sequence")) != sequence or card.get("spacePrefixCode") != space:
            return _failure("ICAFE_SNAPSHOT_IDENTITY_MISMATCH")
        if not _valid_card_shape(card):
            return _failure("ICAFE_SNAPSHOT_INVALID")
        canonical_id = f"{space}-{sequence}"
        properties = card["properties"]
        fields = {
            str(item["propertyName"]): item.get("displayValue")
            for item in properties
            if isinstance(item, dict) and isinstance(item.get("propertyName"), str)
        }
        acceptance = [
            value
            for name, value in fields.items()
            if "accept" in name.lower() or "验收" in name
        ]
        responsible = card["responsiblePeople"]
        snapshot = {
            "canonical_card_id": canonical_id,
            "title": card.get("title", ""),
            "body": card.get("detail", ""),
            "html": card.get("detail", ""),
            "acceptance": acceptance,
            "fields": fields,
            "attachments": card.get("attachments", []),
            "links": card.get("links", []),
            "status": card.get("status"),
            "type": _type_name(card.get("type")),
            "responsible_people": responsible,
            "created": {
                "user": card.get("createdUser"),
                "time": card.get("createdTime"),
            },
            "modified": {
                "user": card.get("lastModifiedUser"),
                "time": card.get("lastModifiedTime"),
            },
        }
        snapshot["content_hash"] = requirement_snapshot_hash(snapshot)
        return snapshot

    @staticmethod
    def comment_marker(idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"<!-- {_COMMENT_PREFIX}:{digest} -->"

    def comment(self, card_id: str, content: str, idempotency_key: str) -> dict[str, Any]:
        parsed = self._parse_card_id(card_id)
        if parsed is None:
            return _failure("ICAFE_CARD_ID_INVALID")
        if not isinstance(content, str) or not content or not isinstance(idempotency_key, str) or not idempotency_key:
            return _failure("INVALID_INPUT")
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        space, sequence = parsed
        canonical_id = f"{space}-{sequence}"
        marker = self.comment_marker(idempotency_key)
        expected_content = f"{content}\n{marker}"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        operation_key = f"icafe-comment:{canonical_id}:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        prior_intent = self.state.intent_by_idempotency_key(operation_key)
        if prior_intent is not None and prior_intent["payload"].get("content_hash") != content_hash:
            return _failure("ICAFE_COMMENT_CONFLICT")
        completed = self.state.result_by_idempotency_key(operation_key)
        if completed is not None:
            return completed["receipt"]["response"]

        existing = self._find_comment(space, sequence, marker)
        if not existing["ok"]:
            return existing
        if existing.get("comment") is not None:
            if existing["comment"].get("content") != expected_content:
                return _failure("ICAFE_COMMENT_CONFLICT")
            receipt = self._comment_receipt(canonical_id, existing["comment"], duplicate=True)
            if prior_intent is not None:
                return self._persist_comment_receipt(prior_intent, receipt)
            return receipt

        pending = prior_intent
        if pending is not None:
            return _failure("QUERY_REQUIRED", intent_id=pending["intent_id"], retry_allowed=False)
        intent = self.state.intent(
            self.run_id,
            "icafe.comment.create",
            operation_key,
            {
                "card_id": canonical_id,
                "content_hash": content_hash,
                "marker_hash": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            },
        )
        created = self._invoke(
            [
                self.binary,
                "comment",
                "create",
                "--space",
                space,
                "--sequence",
                sequence,
                "--content",
                expected_content,
            ],
            "status",
        )
        if not created["ok"]:
            reconciled = self._find_comment(space, sequence, marker)
            if reconciled["ok"] and reconciled.get("comment") is not None:
                if reconciled["comment"].get("content") != expected_content:
                    return _failure("ICAFE_COMMENT_CONFLICT")
                receipt = self._comment_receipt(canonical_id, reconciled["comment"], duplicate=False)
                return self._persist_comment_receipt(intent, receipt)
            if not _unknown_result(created["reason_code"]) and reconciled["ok"]:
                return self._persist_failure(intent, created["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        verified = self._find_comment(space, sequence, marker)
        if not verified["ok"]:
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        if verified.get("comment") is None:
            return _failure(
                "ICAFE_COMMENT_VERIFICATION_FAILED",
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        if verified["comment"].get("content") != expected_content:
            return _failure("ICAFE_COMMENT_CONFLICT")
        receipt = self._comment_receipt(canonical_id, verified["comment"], duplicate=False)
        return self._persist_comment_receipt(intent, receipt)

    def update_status(self, card_id: str, status: str, expected_current: str) -> dict[str, Any]:
        parsed = self._parse_card_id(card_id)
        if parsed is None:
            return _failure("ICAFE_CARD_ID_INVALID")
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        if not status or not expected_current:
            return _failure("INVALID_INPUT")
        space, sequence = parsed
        canonical_id = f"{space}-{sequence}"
        operation_key = f"icafe-status:{canonical_id}:{expected_current}:{status}"
        completed = self.state.result_by_idempotency_key(operation_key)
        if completed is not None:
            return completed["receipt"]["response"]
        pending = self.state.intent_by_idempotency_key(operation_key)
        prepared = self._prepare_status(space, sequence, status, expected_current)
        if not prepared["ok"]:
            return prepared
        if prepared.get("already_target"):
            if pending is None:
                return _failure("ICAFE_STATUS_CHANGED", current_status=status)
            return self._persist_status_receipt(pending, canonical_id, expected_current, status)
        if pending is not None:
            return _failure("QUERY_REQUIRED", intent_id=pending["intent_id"], retry_allowed=False)
        return self._execute_status(
            space,
            sequence,
            canonical_id,
            status,
            expected_current,
            operation_key,
        )

    def _execute_status(
        self,
        space: str,
        sequence: str,
        canonical_id: str,
        status: str,
        expected_current: str,
        operation_key: str,
    ) -> dict[str, Any]:
        intent = self.state.intent(
            self.run_id,
            "icafe.card.update-status",
            operation_key,
            {"card_id": canonical_id, "expected_current": expected_current, "target_status": status},
        )
        updated = self._invoke(
            [
                self.binary,
                "card",
                "update",
                "--space",
                space,
                "--sequence",
                sequence,
                "--status",
                status,
            ],
            "code",
        )
        if not updated["ok"]:
            if not _unknown_result(updated["reason_code"]):
                return self._persist_failure(intent, updated["reason_code"])
            return _failure("QUERY_REQUIRED", intent_id=intent["intent_id"], retry_allowed=False)
        card_check = self._invoke(
            [
                self.binary,
                "card",
                "get",
                "--space",
                space,
                "--sequence",
                sequence,
                "--brief",
            ],
            "code",
        )
        final = self._current_status(space, sequence)
        cards = card_check.get("payload", {}).get("cards") if card_check["ok"] else None
        card_status = cards[0].get("status") if isinstance(cards, list) and len(cards) == 1 and isinstance(cards[0], dict) else None
        if not final["ok"] or card_status != status or final.get("status") != status:
            return _failure(
                "ICAFE_STATUS_VERIFICATION_FAILED",
                intent_id=intent["intent_id"],
                retry_allowed=False,
            )
        return self._persist_status_receipt(intent, canonical_id, expected_current, status)

    def _prepare_status(
        self,
        space: str,
        sequence: str,
        status: str,
        expected_current: str,
    ) -> dict[str, Any]:
        current = self._current_status(space, sequence)
        if not current["ok"]:
            return current
        current_name = current["status"]
        if current_name == status:
            return {"ok": True, "reason_code": "OK", "already_target": True}
        if current_name != expected_current:
            return _failure("ICAFE_STATUS_CHANGED", current_status=current_name)
        reachable = self._invoke(
            [
                self.binary,
                "card",
                "next-statuses",
                "--space",
                space,
                "--sequence",
                sequence,
            ],
            "status",
        )
        if not reachable["ok"]:
            return reachable
        choices = reachable["payload"].get("result")
        names = {
            item.get("statusName")
            for item in choices
            if isinstance(item, dict) and isinstance(item.get("statusName"), str)
        } if isinstance(choices, list) else set()
        if status not in names:
            return _failure("ICAFE_STATUS_UNREACHABLE")
        return {"ok": True, "reason_code": "OK", "already_target": False}

    @staticmethod
    def close_input_hash(card_id: str, status: str, reason: str, knowledge_url: str) -> str:
        canonical_url = _canonical_knowledge_url(knowledge_url)
        return _hash(
            {
                "card_id": card_id,
                "status": status,
                "reason": reason,
                "knowledge_url": canonical_url or knowledge_url,
            }
        )

    def close(
        self,
        card_id: str,
        status: str,
        reason: str,
        knowledge_url: str,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in self.terminal_statuses:
            return _failure("ICAFE_DELETE_UNSUPPORTED")
        if not reason:
            return _failure("ICAFE_DELETE_UNSUPPORTED")
        canonical_url = _canonical_knowledge_url(knowledge_url)
        if canonical_url is None:
            return _failure("ICAFE_KNOWLEDGE_URL_INVALID")
        approval_id = approval.get("approval_id") if isinstance(approval, dict) else None
        record = self.approval_lookup(approval_id) if self.approval_lookup is not None and isinstance(approval_id, str) else None
        if (
            record is None
            or record.get("action") != "ICAFE_CLOSE"
            or record.get("effective_decision") != "APPROVE"
        ):
            return _failure("ICAFE_APPROVAL_REQUIRED")
        expected_hash = self.close_input_hash(card_id, status, reason, canonical_url)
        if record.get("input_hash") != expected_hash:
            return _failure("INPUT_HASH_MISMATCH")
        persistence = self._persistence()
        if persistence is not None:
            return persistence
        parsed = self._parse_card_id(card_id)
        if parsed is None:
            return _failure("ICAFE_CARD_ID_INVALID")
        space, sequence = parsed
        comment_content = f"Close/cancel reason: {reason}\nKnowledge: {canonical_url}"
        reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        comment_hash = hashlib.sha256(comment_content.encode("utf-8")).hexdigest()
        close_key = (
            f"icafe-close:{card_id}:{status}:{expected_hash}:"
            f"{reason_hash}:{url_hash}:{comment_hash}"
        )
        completed = self.state.result_by_idempotency_key(close_key)
        if completed is not None:
            return completed["receipt"]["response"]

        close_intent = self.state.intent_by_idempotency_key(close_key)
        current = self.snapshot(card_id)
        if current.get("reason_code") and current.get("reason_code") != "OK":
            return current
        current_status = str(current.get("status", ""))

        if close_intent is None:
            if current_status == status:
                return _failure("ICAFE_STATUS_CHANGED", current_status=current_status)
            prepared = self._prepare_status(space, sequence, status, current_status)
            if not prepared["ok"]:
                return prepared
            if prepared.get("already_target"):
                return _failure("ICAFE_STATUS_CHANGED", current_status=status)
            close_intent = self.state.intent(
                self.run_id,
                "icafe.card.close",
                close_key,
                {
                    "card_id": card_id,
                    "target_status": status,
                    "expected_current": current_status,
                    "approved_input_hash": expected_hash,
                    "reason_hash": reason_hash,
                    "knowledge_url_hash": url_hash,
                    "comment_content_hash": comment_hash,
                },
            )
            expected_current = current_status
        else:
            expected_current = close_intent["payload"].get("expected_current")
            if not isinstance(expected_current, str) or not expected_current:
                return _failure("ICAFE_CLOSE_INTENT_INVALID")
            if current_status not in {expected_current, status}:
                return _failure("ICAFE_STATUS_CHANGED", current_status=current_status)
            if current_status == expected_current:
                operation_key = (
                    f"icafe-status:{card_id}:{expected_current}:{status}"
                )
                pending_status = self._pending(operation_key)
                if pending_status is not None:
                    return _failure(
                        "QUERY_REQUIRED",
                        intent_id=pending_status["intent_id"],
                        retry_allowed=False,
                    )
                prepared = self._prepare_status(
                    space, sequence, status, expected_current
                )
                if not prepared["ok"]:
                    return prepared
                if prepared.get("already_target"):
                    current_status = status

        status_receipt = self._close_status_receipt(
            space,
            sequence,
            card_id,
            status,
            expected_current,
            current_status,
        )
        if not status_receipt.get("ok"):
            return self._complete_close_failure(close_intent, status_receipt)
        comment = self.comment(
            card_id,
            comment_content,
            f"close:{expected_hash}",
        )
        if not comment.get("ok"):
            return self._complete_close_failure(close_intent, comment)
        response = {
            "ok": True,
            "reason_code": "OK",
            "card_id": card_id,
            "status": status,
            "comment_receipt": comment,
            "status_receipt": status_receipt,
            "evidence_refs": comment["evidence_refs"] + status_receipt["evidence_refs"],
        }
        self.state.receipt(
            close_intent["intent_id"], response, response["evidence_refs"]
        )
        return response

    def _close_status_receipt(
        self,
        space: str,
        sequence: str,
        card_id: str,
        status: str,
        expected_current: str,
        current_status: str,
    ) -> dict[str, Any]:
        operation_key = f"icafe-status:{card_id}:{expected_current}:{status}"
        completed = self.state.result_by_idempotency_key(operation_key)
        if completed is not None:
            return completed["receipt"]["response"]
        pending = self.state.intent_by_idempotency_key(operation_key)
        if current_status == status:
            if pending is None:
                pending = self.state.intent(
                    self.run_id,
                    "icafe.card.update-status.reconcile",
                    operation_key,
                    {
                        "card_id": card_id,
                        "expected_current": expected_current,
                        "target_status": status,
                    },
                )
            return self._persist_status_receipt(
                pending, card_id, expected_current, status
            )
        if pending is not None:
            return _failure(
                "QUERY_REQUIRED",
                intent_id=pending["intent_id"],
                retry_allowed=False,
            )
        return self._execute_status(
            space,
            sequence,
            card_id,
            status,
            expected_current,
            operation_key,
        )

    def _complete_close_failure(
        self, close_intent: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        if result.get("reason_code") == "QUERY_REQUIRED":
            return result
        evidence = result.get("evidence_refs")
        safe_evidence = evidence if isinstance(evidence, list) else []
        self.state.receipt(close_intent["intent_id"], result, safe_evidence)
        return result

    def _ensure_preflight(self) -> dict[str, Any] | None:
        if self._preflight_complete or not self.preflight or getattr(self.transport, "skip_preflight", False):
            return None
        try:
            self.transport.run([self.binary, "version"], expect_json=False)
            self.transport.run([self.binary, "login", "status"], expect_json=False)
        except CliTransportError as error:
            return _failure(error.reason_code, diagnostic=error.diagnostic)
        self._preflight_complete = True
        return None

    def _invoke(self, argv: list[str], business_field: str) -> dict[str, Any]:
        preflight = self._ensure_preflight()
        if preflight is not None:
            return preflight
        try:
            payload = self.transport.run(argv, business_field=business_field)
        except CliTransportError as error:
            return _failure(error.reason_code, diagnostic=error.diagnostic)
        status = payload.get(business_field)
        if status != 200 or payload.get("success") is False:
            return _failure(_business_reason(status))
        return {"ok": True, "reason_code": "OK", "payload": payload}

    def _find_comment(self, space: str, sequence: str, marker: str) -> dict[str, Any]:
        response = self._invoke(
            [
                self.binary,
                "comment",
                "get",
                "--space",
                space,
                "--sequence",
                sequence,
            ],
            "status",
        )
        if not response["ok"]:
            return response
        comments = response["payload"].get("result")
        if not isinstance(comments, list):
            return _failure("ICAFE_COMMENT_QUERY_INVALID")
        matches = [
            item
            for item in comments
            if isinstance(item, dict)
            and not item.get("isDeleted", False)
            and marker in str(item.get("content", ""))
        ]
        if len(matches) > 1:
            return _failure("ICAFE_COMMENT_CONFLICT")
        return {"ok": True, "reason_code": "OK", "comment": matches[0] if matches else None}

    def _current_status(self, space: str, sequence: str) -> dict[str, Any]:
        response = self._invoke(
            [
                self.binary,
                "card",
                "current-status",
                "--space",
                space,
                "--sequence",
                sequence,
            ],
            "status",
        )
        if not response["ok"]:
            return response
        name = response["payload"].get("statusName")
        if not isinstance(name, str) or not name:
            return _failure("ICAFE_STATUS_INVALID")
        return {"ok": True, "reason_code": "OK", "status": name}

    def _comment_receipt(self, card_id: str, comment: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        comment_id = comment.get("id")
        if not isinstance(comment_id, (int, str)) or isinstance(comment_id, bool):
            return _failure("ICAFE_COMMENT_VERIFICATION_FAILED")
        reference = f"icafe:{card_id}/{comment_id}"
        return {
            "ok": True,
            "reason_code": "OK",
            "card_id": card_id,
            "comment_id": comment_id,
            "duplicate": duplicate,
            "evidence_refs": [reference],
        }

    def _persist_comment_receipt(self, intent: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        if not receipt.get("ok"):
            return receipt
        self.state.receipt(
            intent["intent_id"],
            receipt,
            receipt["evidence_refs"],
        )
        return receipt

    def _persist_status_receipt(
        self,
        intent: dict[str, Any],
        card_id: str,
        previous_status: str,
        status: str,
    ) -> dict[str, Any]:
        status_id = hashlib.sha256(f"{previous_status}\0{status}".encode()).hexdigest()[:16]
        evidence = [f"icafe:{card_id}/status-{status_id}"]
        response = {
            "ok": True,
            "reason_code": "OK",
            "card_id": card_id,
            "previous_status": previous_status,
            "status": status,
            "evidence_refs": evidence,
        }
        self.state.receipt(intent["intent_id"], response, evidence)
        return response

    def _persist_failure(self, intent: dict[str, Any], reason_code: str) -> dict[str, Any]:
        response = _failure(reason_code)
        self.state.receipt(intent["intent_id"], response, [])
        return response

    def _pending(self, idempotency_key: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.state.pending_intents(self.run_id)
                if item["idempotency_key"] == idempotency_key
            ),
            None,
        )

    def _persistence(self) -> dict[str, Any] | None:
        if self.state is None or not isinstance(self.run_id, str) or not self.run_id:
            return _failure("PERSISTENCE_REQUIRED")
        return None

    @staticmethod
    def _parse_card_id(card_id: str) -> tuple[str, str] | None:
        if not isinstance(card_id, str):
            return None
        match = _CARD_ID.fullmatch(card_id)
        if match is None:
            return None
        return match.group(1), match.group(2)


def _failure(reason_code: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, **details}


def _hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_knowledge_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "ku.baidu-int.com"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or _KU_PATH.fullmatch(parsed.path) is None
    ):
        return None
    return f"https://ku.baidu-int.com{parsed.path}"


def _type_name(value: Any) -> Any:
    return value.get("name") if isinstance(value, dict) else value


def _valid_card_shape(card: dict[str, Any]) -> bool:
    if not all(isinstance(card.get(key), str) for key in ("title", "detail", "status")):
        return False
    card_type = card.get("type")
    if not isinstance(card_type, dict) or not isinstance(card_type.get("name"), str):
        return False
    if not isinstance(card.get("responsiblePeople"), list) or not all(
        isinstance(person, dict) and isinstance(person.get("username"), str)
        for person in card["responsiblePeople"]
    ):
        return False
    if not isinstance(card.get("properties"), list) or not all(
        isinstance(field, dict) and isinstance(field.get("propertyName"), str)
        for field in card["properties"]
    ):
        return False
    for key in ("attachments", "links"):
        if key in card and not isinstance(card[key], list):
            return False
    if not isinstance(card.get("createdUser"), dict) or not isinstance(card.get("lastModifiedUser"), dict):
        return False
    if not isinstance(card.get("createdTime"), str) or not isinstance(card.get("lastModifiedTime"), str):
        return False
    return True


def _unknown_result(reason_code: str) -> bool:
    return reason_code in {"CLI_TIMEOUT", "CLI_PROCESS_FAILED", "CLI_INVALID_JSON"}


def _business_reason(status: Any) -> str:
    if status == 100:
        return "AUTH_REQUIRED"
    if status == 101:
        return "PERMISSION_DENIED"
    if status in {304, 404}:
        return "OBJECT_NOT_FOUND"
    if status in {306, 401, 601, 603, 704, 901, 1011}:
        return "INVALID_INPUT"
    return "CLI_BUSINESS_FAILURE"
