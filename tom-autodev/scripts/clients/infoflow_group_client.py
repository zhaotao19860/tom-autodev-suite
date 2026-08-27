from __future__ import annotations

from pathlib import Path
from typing import Any

from cli_transport import CliTransport


class InfoflowGroupClient:
    """Thin, injectable boundary around the installed Infoflow group scripts."""

    def __init__(self, transport: Any | None = None, script_root: Path | str | None = None):
        self.transport = transport or CliTransport()
        self.script_root = Path(
            script_root or Path.home() / ".comate" / "skills" / "infoflow-message-group" / "scripts"
        )

    def create_or_reuse(self, group_request: dict[str, Any]) -> dict[str, Any]:
        group_name = _required(group_request, "group_name")
        owner = _email(_required(group_request, "owner"))
        members = group_request.get("member_snapshot")
        if not isinstance(members, list) or not members:
            raise ValueError("GROUP_MEMBERS_INVALID")
        normalized_members = [_email(member) for member in members]
        if group_request.get("friendlyLevel") != 3:
            raise ValueError("GROUP_FRIENDLY_LEVEL_INVALID")

        self.transport.run([str(self.script_root / "setup.sh"), "--check"], expect_json=False)
        response = self.transport.run(
            [
                str(self.script_root / "group_create.sh"),
                group_name,
                owner,
                ",".join(normalized_members),
                "3",
            ]
        )
        return _group_receipt(response, group_name)

    def send_markdown(
        self, group_id: str, content: str, at_users: list[str], idempotency_key: str
    ) -> dict[str, Any]:
        if not isinstance(group_id, str) or not group_id.isdigit():
            raise ValueError("GROUP_ID_INVALID")
        if not isinstance(content, str) or not content:
            raise ValueError("MESSAGE_CONTENT_INVALID")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("IDEMPOTENCY_KEY_INVALID")
        recipients = [_email(member) for member in at_users]
        for recipient in recipients:
            if f"@{recipient}" not in content:
                raise ValueError("MESSAGE_MENTION_MISMATCH")
        response = self.transport.run(
            [
                str(self.script_root / "msg_send_group.sh"),
                group_id,
                "MD",
                content,
                ",".join(recipients),
            ]
        )
        return _message_receipt(response, group_id, idempotency_key)

    def reconcile_group(self, group_request: dict[str, Any]) -> dict[str, Any] | None:
        """The group scripts have no lookup-by-client-key API, so never replay a pending create."""
        return None

    def reconcile_message(self, group_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """The group scripts have no lookup-by-client-key API, so never replay a pending send."""
        return None


def _required(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise ValueError("GROUP_REQUEST_INVALID")
    return result


def _email(value: Any) -> str:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("MEMBER_CONFIRMATION_REQUIRED")
    local, domain = value.split("@", 1)
    if not local or not domain or "." not in domain or any(char.isspace() for char in value):
        raise ValueError("MEMBER_CONFIRMATION_REQUIRED")
    return value


def _group_receipt(response: dict[str, Any], group_name: str) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else None
    source = data if isinstance(data, dict) else response
    group_id = source.get("groupId") or source.get("group_id") or source.get("chatId")
    if group_id is None:
        raise ValueError("GROUP_CREATE_RESPONSE_INVALID")
    result = {"group_id": str(group_id), "group_name": group_name}
    bot_id = source.get("botId") or source.get("bot_id")
    if bot_id is not None:
        result["bot_id"] = str(bot_id)
    return result


def _message_receipt(response: dict[str, Any], group_id: str, idempotency_key: str) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else None
    source = data if isinstance(data, dict) else response
    message_id = source.get("messageId") or source.get("msgId") or source.get("message_id")
    if message_id is None:
        raise ValueError("MESSAGE_RESPONSE_INVALID")
    return {
        "message_id": str(message_id),
        "group_id": group_id,
        "idempotency_key": idempotency_key,
    }
