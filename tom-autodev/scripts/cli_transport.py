from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any


_SENSITIVE_FLAGS = frozenset(
    {
        "--authorization",
        "--content",
        "--credential",
        "--detail",
        "--operation",
        "--operations",
        "--password",
        "--private-key",
        "--secret",
        "--token",
    }
)
_CREDENTIAL_FILE = re.compile(r"(?:auth|credential|password|private[-_.]?key|secret|token)", re.I)
_SECRET_ENVIRONMENT = re.compile(r"(?:authorization|credential|password|private[_-]?key|secret|token)", re.I)


class CliTransportError(RuntimeError):
    def __init__(self, reason_code: str, diagnostic: dict[str, Any]):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.diagnostic = diagnostic


class CliTransport:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 30,
        stderr_limit: int = 4096,
        environment: Mapping[str, str] | None = None,
    ):
        if timeout_seconds <= 0 or stderr_limit < 0:
            raise ValueError("CLI_TRANSPORT_CONFIG_INVALID")
        exported = dict(environment or {})
        if any(_SECRET_ENVIRONMENT.search(name) for name in exported):
            raise ValueError("CLI_ENVIRONMENT_SECRET_REJECTED")
        if not all(isinstance(name, str) and isinstance(value, str) for name, value in exported.items()):
            raise ValueError("CLI_TRANSPORT_CONFIG_INVALID")
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.stderr_limit = stderr_limit
        self.environment = exported
        self.last_diagnostic: dict[str, Any] = {}

    def run(
        self,
        argv: Sequence[str],
        *,
        business_field: str | None = None,
        expected_value: Any = 200,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        command = self._validate_argv(argv)
        diagnostic = {"argv": _redact_argv(command)}
        environment = os.environ.copy()
        environment.update(self.environment)
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            diagnostic.update(_stderr_diagnostic(error.stderr, self.stderr_limit))
            self.last_diagnostic = diagnostic
            raise CliTransportError("CLI_TIMEOUT", diagnostic) from None
        except OSError:
            self.last_diagnostic = diagnostic
            raise CliTransportError("CLI_NOT_FOUND", diagnostic) from None

        diagnostic["exit_code"] = completed.returncode
        diagnostic.update(_stderr_diagnostic(completed.stderr, self.stderr_limit))
        self.last_diagnostic = diagnostic
        if completed.returncode != 0:
            raise CliTransportError(
                _process_reason(completed.returncode, completed.stderr), diagnostic
            )
        if not expect_json:
            return {"ok": True}
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise CliTransportError("CLI_INVALID_JSON", diagnostic) from None
        if not isinstance(payload, dict):
            raise CliTransportError("CLI_INVALID_JSON", diagnostic)
        if business_field is not None:
            if payload.get(business_field) != expected_value:
                raise CliTransportError(_business_reason(payload.get(business_field)), diagnostic)
            if payload.get("success") is False:
                raise CliTransportError("CLI_BUSINESS_FAILURE", diagnostic)
        return payload

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> list[str]:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("CLI_ARGV_INVALID")
        command = list(argv)
        if not all(isinstance(argument, str) and argument for argument in command):
            raise ValueError("CLI_ARGV_INVALID")
        return command


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    previous = ""
    for argument in argv:
        if hide_next:
            redacted.append("[REDACTED]")
            hide_next = False
        elif argument in _SENSITIVE_FLAGS:
            redacted.append(argument)
            hide_next = True
        elif previous in {"--file", "--md-file"} and _CREDENTIAL_FILE.search(argument):
            redacted.append("[REDACTED]")
        elif any(argument.startswith(f"{flag}=") for flag in _SENSITIVE_FLAGS):
            redacted.append(f"{argument.split('=', 1)[0]}=[REDACTED]")
        else:
            redacted.append(argument)
        previous = argument
    return redacted


def _stderr_diagnostic(stderr: str | bytes | None, limit: int) -> dict[str, Any]:
    if stderr is None:
        raw = b""
    elif isinstance(stderr, bytes):
        raw = stderr
    else:
        raw = stderr.encode("utf-8", errors="replace")
    bounded = raw[:limit]
    return {
        "stderr_bytes": len(raw),
        "stderr_sha256": hashlib.sha256(bounded).hexdigest(),
        "stderr_truncated": len(raw) > len(bounded),
    }


def _process_reason(exit_code: int, stderr: str | bytes | None) -> str:
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    status = None
    error_code = None
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            status = decoded.get("status")
            error_code = decoded.get("error")
    except json.JSONDecodeError:
        pass
    if status == 401:
        return "AUTH_REQUIRED"
    if status == 403:
        return "PERMISSION_DENIED"
    if status == 404:
        return "OBJECT_NOT_FOUND"
    if status == 400:
        return "INVALID_INPUT"
    if exit_code == 2:
        return "INVALID_INPUT"
    if exit_code == 1:
        return "AUTH_REQUIRED" if error_code == "auth_failed" else "INVALID_INPUT"
    return "CLI_PROCESS_FAILED"


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
