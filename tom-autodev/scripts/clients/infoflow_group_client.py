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
                "idempotency_key": group_request.get("idempotency_key"),
            }
        )
        result = {"group_id": receipt["group_id"], "group_name": group_name}
        # The platform does not promise to echo the robot identity in a successful
        # group-create response. The gateway already put the bot in robotList, so a
        # missing bot_id is not evidence that the group create failed.
        if isinstance(receipt.get("bot_id"), str) and receipt["bot_id"]:
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
        """Read the gateway's durable create receipt without replaying the write."""
        key = group_request.get("idempotency_key")
        query = getattr(self.bot, "query_group_receipt", None)
        if not isinstance(key, str) or not key or not callable(query):
            return None
        receipt = query(key)
        if not isinstance(receipt, dict):
            return None
        group_id = receipt.get("group_id")
        if not isinstance(group_id, str) or not group_id.isdigit():
            return None
        recorded_name = receipt.get("group_name")
        if recorded_name is not None and recorded_name != group_request.get("group_name"):
            return None
        result = {"group_id": group_id, "group_name": group_request["group_name"]}
        if isinstance(receipt.get("bot_id"), str) and receipt["bot_id"]:
            result["bot_id"] = receipt["bot_id"]
        return result

    def verify_group(
        self, group_id: str, expected_name: str, expected_members: list[str] | None = None
    ) -> dict[str, Any] | None:
        verify = getattr(self.bot, "verify_group", None)
        if not callable(verify):
            return None
        result = verify(group_id)
        if not isinstance(result, dict):
            return None
        if result.get("group_id") != group_id or result.get("group_name") != expected_name:
            return None
        if result.get("members_queried") is not True:
            return None
        if expected_members:
            def member_key(value: str) -> str:
                return value.split("@", 1)[0]

            actual = {
                member_key(value) for value in result.get("member_emails", [])
                if isinstance(value, str) and value
            }
            expected = {
                member_key(value) for value in expected_members
                if isinstance(value, str) and value
            }
            if not expected.issubset(actual):
                return None
        return {"group_id": group_id, "group_name": expected_name, "members_queried": True}

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
