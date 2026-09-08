from __future__ import annotations

from typing import Any

from clients.infoflow_bot_client import InfoflowBotClient


class InfoflowGroupClient:
    """Collaboration group operations, performed by the bot through its gateway.

    The `infoflow-message-group` scripts need `INFOFLOW_TOKEN` exported into their
    environment, and the CLI boundary here deliberately refuses to carry secrets
    (`CLI_ENVIRONMENT_SECRET_REJECTED`), so group writes go through the same
    localhost gateway that already owns the bot credentials for approvals.
    """

    def __init__(self, bot_client: Any | None = None):
        self.bot = bot_client or InfoflowBotClient()

    def create_or_reuse(self, group_request: dict[str, Any]) -> dict[str, Any]:
        group_name = _required(group_request, "group_name")
        owner = _email(_required(group_request, "owner"))
        members = group_request.get("member_snapshot")
        if not isinstance(members, list) or not members:
            raise ValueError("GROUP_MEMBERS_INVALID")
        if group_request.get("friendlyLevel") != 3:
            raise ValueError("GROUP_FRIENDLY_LEVEL_INVALID")
        receipt = self.bot.create_group(
            {
                "group_name": group_name,
                "owner": owner,
                "members": [_email(member) for member in members],
                "friendly_level": 3,
            }
        )
        result = {"group_id": receipt["group_id"], "group_name": group_name}
        if not isinstance(receipt.get("bot_id"), str) or not receipt["bot_id"]:
            raise ValueError("BOT_AGENT_ID_UNAVAILABLE")
        result["bot_id"] = receipt["bot_id"]
        return result

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
        receipt = self.bot.send_group_markdown(group_id, content, recipients)
        return {
            "message_id": receipt["message_id"],
            "group_id": group_id,
            "idempotency_key": idempotency_key,
        }

    def reconcile_group(self, group_request: dict[str, Any]) -> dict[str, Any] | None:
        """Infoflow exposes no lookup by group name, so never replay a pending create."""
        return None

    def reconcile_message(self, group_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """Infoflow exposes no lookup by client key, so never replay a pending send."""
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
