from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_DEFAULT_URL = "http://127.0.0.1:18791"
_REGISTRY = "http://registry.npm.baidu-int.com"


class InfoflowBotClient:
    """Approval delivery through the bot's own gateway.

    The request has to be sent by the bot app, not by a personal account: only the
    app holds the inbound WebSocket, so anything else would produce a request that
    can never be answered automatically. The gateway resolves the bot credentials
    itself (env, then `~/.infoflow_config`) and this client only speaks localhost
    HTTP, so no token is ever exported into a shell or handled here.

    A missing gateway is started on demand — installing `node_modules` first if the
    checkout has never run — because an approval must not fail just because the
    long-lived process was not up yet.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        gateway_dir: Path | str | None = None,
        journal_path: Path | str | None = None,
        log_path: Path | str | None = None,
        opener: Any | None = None,
        runner: Any | None = None,
        launcher: Any | None = None,
        sleeper: Any | None = None,
        startup_attempts: int = 30,
    ):
        self.base_url = (base_url or os.environ.get("TOM_AUTODEV_INFOFLOW_GATEWAY_URL") or _DEFAULT_URL).rstrip("/")
        self.gateway_dir = Path(
            gateway_dir
            or Path(__file__).resolve().parents[2] / "infoflow-gateway"
        )
        self.journal_path = Path(journal_path).expanduser() if journal_path is not None else None
        self.log_path = Path(log_path or Path.home() / ".tom-autodev" / "infoflow-gateway.log")
        self.opener = opener or urllib.request.urlopen
        self.runner = runner or subprocess.run
        self.launcher = launcher or subprocess.Popen
        self.sleeper = sleeper or time.sleep
        self.startup_attempts = startup_attempts

    def send_markdown(self, recipients: list[str], content: str) -> dict[str, Any]:
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("NOTIFY_RECIPIENTS_INVALID")
        if not isinstance(content, str) or not content:
            raise ValueError("MESSAGE_CONTENT_INVALID")
        self.ensure_ready()
        try:
            response = self._request(
                "POST",
                "/notify",
                {"recipients": [_uuap(recipient) for recipient in recipients], "content": content},
            )
        except urllib.error.HTTPError:
            # The gateway answers a rejected send with a non-2xx status; the message
            # did not land, so this must never look like a delivered request.
            raise ValueError("MESSAGE_REJECTED:GATEWAY") from None
        except (urllib.error.URLError, OSError):
            raise ValueError("INFOFLOW_GATEWAY_UNAVAILABLE") from None
        if response is None or response.get("ok") is not True:
            raise ValueError("MESSAGE_REJECTED:GATEWAY")
        identity = response.get("message_key")
        if not isinstance(identity, str) or not identity:
            raise ValueError("MESSAGE_RESPONSE_INVALID")
        return {"message_key": identity, "recipients": sorted(recipients)}

    def create_group(self, request: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/group/create", request)
        identity = response.get("group_id")
        if not isinstance(identity, str) or not identity.isdigit():
            raise ValueError("GROUP_CREATE_RESPONSE_INVALID")
        receipt = {"group_id": identity, "group_name": response.get("group_name")}
        if isinstance(response.get("bot_id"), str) and response["bot_id"]:
            receipt["bot_id"] = response["bot_id"]
        return receipt

    def send_group_markdown(
        self, group_id: str, content: str, at_users: list[str]
    ) -> dict[str, Any]:
        response = self._post(
            "/group/message",
            {
                "group_id": group_id,
                "content": content,
                # Group mentions are matched against the message body by the caller,
                # so the ids are passed through exactly as written there.
                "at_users": list(at_users),
            },
        )
        identity = response.get("message_key")
        if not isinstance(identity, str) or not identity:
            raise ValueError("MESSAGE_RESPONSE_INVALID")
        return {"message_id": identity, "group_id": group_id}

    def send_approval_card(
        self,
        *,
        approval_id: str,
        target_type: str,
        target_id: str,
        title: str,
        question: str,
        lines: list[str],
    ) -> dict[str, Any]:
        """Send the one-tap variant of a gate as an interactive card.

        The buttons carry the approval id, so a tap arrives in the reply journal as the
        same `APPROVE <approval_id>` a person would otherwise have had to type — the
        decision is still authorized by `clients.infoflow_reply_client` against the
        stored request, nothing here decides anything.
        """
        response = self._post(
            "/card/approval",
            {
                "approval_id": approval_id,
                "target_type": target_type,
                "target_id": target_id,
                "title": title,
                "question": question,
                "lines": list(lines),
            },
        )
        identity = response.get("card_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("CARD_RESPONSE_INVALID")
        return {"card_id": identity, "created": response.get("created") is True}

    def send_choice_card(
        self,
        *,
        decision_id: str,
        target_type: str,
        target_id: str,
        title: str,
        question: str,
        lines: list[str],
        options: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Send a non-gate question as a button card.

        This is weaker than `send_approval_card` on purpose: the answer is scoped only
        by the group it was tapped in, so it must never stand in for an approval gate.
        """
        response = self._post(
            "/card/choice",
            {
                "decision_id": decision_id,
                "target_type": target_type,
                "target_id": target_id,
                "title": title,
                "question": question,
                "lines": list(lines),
                "options": list(options),
            },
        )
        identity = response.get("card_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("CARD_RESPONSE_INVALID")
        return {"card_id": identity, "created": response.get("created") is True}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready()
        try:
            response = self._request("POST", path, payload)
        except urllib.error.HTTPError:
            raise ValueError("MESSAGE_REJECTED:GATEWAY") from None
        except (urllib.error.URLError, OSError):
            raise ValueError("INFOFLOW_GATEWAY_UNAVAILABLE") from None
        if response is None or response.get("ok") is not True:
            raise ValueError("MESSAGE_REJECTED:GATEWAY")
        return response

    def ensure_ready(self) -> dict[str, Any]:
        health = self._health()
        if health is None:
            self._start()
            for _ in range(self.startup_attempts):
                self.sleeper(1)
                health = self._health()
                if health is not None:
                    break
        if health is None:
            raise ValueError("INFOFLOW_GATEWAY_UNAVAILABLE")
        # A gateway journaling somewhere else would leave replies unreadable while the
        # request still looked delivered.
        if self.journal_path is not None and health.get("journal") != str(self.journal_path):
            raise ValueError("INFOFLOW_GATEWAY_JOURNAL_MISMATCH")
        return health

    def _health(self) -> dict[str, Any] | None:
        try:
            response = self._request("GET", "/health", timeout=3)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return response if isinstance(response, dict) and response.get("ok") is True else None

    def _start(self) -> None:
        server = self.gateway_dir / "src" / "bot_gateway.mjs"
        if not server.exists():
            raise ValueError("INFOFLOW_GATEWAY_MISSING")
        if not (self.gateway_dir / "node_modules").exists():
            installed = self.runner(
                ["npm", "install", f"--registry={_REGISTRY}"],
                cwd=str(self.gateway_dir),
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if getattr(installed, "returncode", 1) != 0:
                raise ValueError("INFOFLOW_GATEWAY_INSTALL_FAILED")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if self.journal_path is not None:
            environment["TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL"] = str(self.journal_path)
        # The gateway outlives this command: a run spans many CLI invocations and the
        # WebSocket must stay connected between them.
        with self.log_path.open("a", encoding="utf-8") as log:
            self.launcher(
                ["node", str(server)],
                cwd=str(self.gateway_dir),
                stdout=log,
                stderr=log,
                env=environment,
                start_new_session=True,
            )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 30
    ) -> dict[str, Any] | None:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with self.opener(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise ValueError("MESSAGE_RESPONSE_INVALID") from None
        return parsed if isinstance(parsed, dict) else None


def _uuap(value: Any) -> str:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("MEMBER_CONFIRMATION_REQUIRED")
    local, domain = value.split("@", 1)
    if not local or not domain or "." not in domain or any(char.isspace() for char in value):
        raise ValueError("MEMBER_CONFIRMATION_REQUIRED")
    return local
