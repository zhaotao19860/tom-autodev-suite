from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://10.11.152.208:8701/api/process/ipipe"


class IpipeTransportError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        *,
        status: int | None = None,
        body: str = "",
        transient: bool = False,
        diagnostic: dict[str, Any] | None = None,
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status = status
        self.body = body
        self.transient = transient
        self.diagnostic = diagnostic or {}


class IpipeHttpTransport:
    """Bounded iPipe HTTP transport. Writes are intentionally single-attempt."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        sender: Callable[..., dict[str, Any]] | None = None,
        timeout_seconds: float = 30,
        max_read_attempts: int = 3,
        body_limit: int = 4096,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if timeout_seconds <= 0 or not 1 <= max_read_attempts <= 5 or not 0 <= body_limit <= 65536:
            raise ValueError("IPIPE_TRANSPORT_CONFIG_INVALID")
        self.base_url = base_url.rstrip("/")
        self._token = _load_token(token)
        self.sender = sender or _urllib_sender
        self.timeout_seconds = timeout_seconds
        self.max_read_attempts = max_read_attempts
        self.body_limit = body_limit
        self.sleeper = sleeper

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        verb = method.upper()
        if verb not in {"GET", "POST"} or not endpoint.startswith("/"):
            raise ValueError("IPIPE_REQUEST_INVALID")
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        url = f"{self.base_url}{endpoint}"
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params, doseq=True)}"
        request_headers = {
            "Content-Type": "application/json",
            "User-Agent": "iAPI/1.0.0 (http://iapi.baidu-int.com)",
            "x-ac-Authorization": self._token,
        }
        for key, value in (headers or {}).items():
            if key.lower() in {"authorization", "x-ac-authorization"}:
                raise ValueError("IPIPE_AUTH_HEADER_OVERRIDE_REJECTED")
            request_headers[key] = value
        attempts = self.max_read_attempts if verb == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self.sender(
                    verb, url, request_headers, body, self.timeout_seconds, self.body_limit
                )
                return self._decode_response(response)
            except IpipeTransportError as error:
                sanitized = self._sanitize_error(error)
                if not sanitized.transient or attempt == attempts or verb != "GET":
                    raise sanitized from None
                self.sleeper(0)
            except (OSError, TimeoutError, urllib.error.URLError) as error:
                wrapped = IpipeTransportError(
                    "PIPELINE_TRANSIENT", transient=True,
                    diagnostic={"error_type": type(error).__name__},
                )
                if attempt == attempts or verb != "GET":
                    raise wrapped from None
                self.sleeper(0)
        raise IpipeTransportError("PIPELINE_TRANSIENT", transient=True)

    def _decode_response(self, response: Any) -> Any:
        if not isinstance(response, dict) or not isinstance(response.get("status"), int):
            raise IpipeTransportError("IPIPE_RESPONSE_INVALID")
        status = response["status"]
        payload = response.get("body")
        if status < 200 or status >= 300:
            raise IpipeTransportError(
                _http_reason(status), status=status, body=_body_text(payload),
                transient=status == 429 or status >= 500,
            )
        if isinstance(payload, str):
            try:
                payload = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                raise IpipeTransportError("IPIPE_RESPONSE_INVALID") from None
        if not isinstance(payload, (dict, list)):
            raise IpipeTransportError("IPIPE_RESPONSE_INVALID")
        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            if code not in {0, 200, "0", "200"}:
                raise IpipeTransportError(
                    _business_reason(code), status=status, body=_body_text(payload),
                    transient=code in {429, 500, 502, 503, 504, "429", "500", "502", "503", "504"},
                )
        return payload

    def _sanitize_error(self, error: IpipeTransportError) -> IpipeTransportError:
        clean_body = (error.body or "").replace(self._token, "[REDACTED]")
        raw_token = self._token.removeprefix("Bearer-")
        if raw_token:
            clean_body = clean_body.replace(raw_token, "[REDACTED]")
        clean_body = re.sub(r"(?i)\bBearer-[A-Za-z0-9._~+/=-]+", "[REDACTED]", clean_body)
        excerpt = clean_body[: self.body_limit]
        diagnostic: dict[str, Any] = {}
        if excerpt:
            diagnostic["body_excerpt"] = excerpt
            diagnostic["body_truncated"] = len(clean_body) > len(excerpt)
        if error.status is not None:
            diagnostic["http_status"] = error.status
        return IpipeTransportError(
            error.reason_code, status=error.status, transient=error.transient, diagnostic=diagnostic
        )


class IpipeApiClient:
    """iPipe API shapes used by the run-bound runtime."""

    def __init__(self, transport: IpipeHttpTransport, *, current_user: str):
        if not isinstance(current_user, str) or not current_user.strip():
            raise ValueError("IPIPE_CURRENT_USER_REQUIRED")
        self.transport = transport
        self.current_user = current_user.strip()

    def get_pipeline_by_id(self, pipeline_id: str) -> dict[str, Any]:
        # The gateway 404s unless the empty pipelineConfId/user/token params are present.
        value = self.transport.request(
            "GET",
            f"/api/ipipe/v10/pipeline/conf/{pipeline_id}",
            params={"pipelineConfId": "", "user": "", "token": "", "brief": ""},
        )
        return _entity(value)

    def get_pipeline_by_name(self, module: str, name: str) -> list[dict[str, Any]]:
        value = self.transport.request("GET", "/api/agile/v1/pipelineConfs/pipelineConfInfo", params={"module": module, "pipelineName": name})
        return _entities(value)

    def pipelines_by_module(self, module: str) -> list[dict[str, Any]]:
        return _entities(self.transport.request("GET", "/api/ipipe/v10/pipeline/getPipelineInfoByModule", params={"module": module}))

    def builds_by_revision(self, module: str, revision: str, pipeline_id: str) -> list[dict[str, Any]]:
        value = self.transport.request(
            "GET", "/api/rest/v10/pipeline-build/builds/revision",
            params={"module": module, "revision": revision, "pipelineConfId": pipeline_id, "embed": "trigger,stageBuilds", "_limit": 20},
        )
        return _entities(value)

    def recent_builds(self, module: str, pipeline_id: str) -> list[dict[str, Any]]:
        value = self.transport.request("GET", "/api/rest/v10/pipeline-build/builds", params={"module": module, "pipelineConfId": pipeline_id, "_embed": "trigger,stageBuilds,params", "_limit": 20})
        return _entities(value)

    def build_by_id(self, build_id: str) -> dict[str, Any]:
        return _entity(self.transport.request("GET", f"/api/rest/v10/pipeline-build/{build_id}", params={"_embed": "trigger,stageBuilds,params", "_include": "ext.simpleCommitInfo"}))

    def pipeline_stage_info(self, build_id: str) -> list[dict[str, Any]]:
        value = self.transport.request(
            "GET",
            "/api/agile/v1/pipelineBuilds/pipelineBuildInfos",
            params={"pipelineBuildId": build_id, "currentUser": self.current_user},
        )
        return _entities(value)

    def failed_jobs(self, build_id: str) -> list[dict[str, Any]]:
        value = self.transport.request(
            "GET",
            "/api/agile/v1/pipelineBuilds/jobBuilds",
            params={
                "pipelineBuildId": build_id,
                "jobStatus": "FAIL",
                "currentUser": self.current_user,
            },
        )
        return _entities(value)

    def stage_detail(self, stage_id: str) -> Any:
        return self.transport.request(
            "GET",
            f"/api/rest/v10/stage-build/{stage_id}/stageAndRealJobBuilds",
            params={"currentUser": self.current_user},
        )

    def release_info(self, module: str, branch: str) -> list[dict[str, Any]]:
        value = self.transport.request("GET", "/api/agile/getReleaseInfo", params={"module": module, "branch": branch, "offset": 0, "limit": 20})
        return _entities(value)

    def trigger_by_revision(self, pipeline_id: str, revision: str, parameters: dict[str, Any]) -> dict[str, Any]:
        value = self.transport.request(
            "POST",
            f"/api/rest/v10/pipeline-build/{pipeline_id}/revision",
            params={
                "token": "",
                "user": self.current_user,
                "pipelineConfId": pipeline_id,
                "revision": revision,
            },
            body={"triggerUser": self.current_user, **parameters},
        )
        return _entity(value)

    def manual_execute_stage(self, stage_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        value = self.transport.request(
            "POST",
            "/api/agile/v1/stageBuilds/build",
            params={"stageBuildId": stage_id, "username": self.current_user},
            body=dict(parameters),
            headers={"AGILE-PLAT-NAME": "", "AGILE-PLAT-TOKEN": ""},
        )
        return _entity(value)


class IpipeClient:
    """Compatibility adapter for earlier callers; new code uses IpipeRuntime."""

    def __init__(
        self,
        transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        *,
        runtime: Any | None = None,
    ):
        if transport is not None or runtime is None:
            raise ValueError("IPIPE_RUNTIME_REQUIRED")
        self.runtime = runtime

    def discover(self, profile: dict[str, Any], revision_set: dict[str, Any]) -> dict[str, Any]:
        if self.runtime is None:
            raise RuntimeError("IPIPE_RUNTIME_REQUIRED")
        return self.runtime.discover(profile, revision_set)

    def trigger(
        self,
        profile: dict[str, Any],
        revision_set: dict[str, Any],
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if approval is None:
            raise ValueError("APPROVAL_REQUIRED:G8")
        return self.runtime.trigger(profile, revision_set, approval)

    def status(self, build_id: str) -> dict[str, Any]:
        raise RuntimeError("IPIPE_MONITOR_DEADLINE_REQUIRED")

    def monitor(self, build_id: str, deadline: str) -> dict[str, Any]:
        if self.runtime is None:
            raise RuntimeError("IPIPE_RUNTIME_REQUIRED")
        return self.runtime.monitor(build_id, deadline)

    def rerun(
        self,
        build_or_stage: str,
        stage_or_approval: str | dict[str, Any],
        *,
        approved: bool | None = None,
    ) -> dict[str, Any]:
        if not isinstance(stage_or_approval, dict) or approved is not None:
            raise ValueError("APPROVAL_REQUIRED:G8")
        return self.runtime.rerun(build_or_stage, stage_or_approval)

    def verify_release(self, build_id: str, revision_set: dict[str, Any]) -> dict[str, Any]:
        if self.runtime is None:
            raise RuntimeError("IPIPE_RUNTIME_REQUIRED")
        return self.runtime.verify_release(build_id, revision_set)


def _load_token(explicit: str | None) -> str:
    value = explicit or os.environ.get("COMATE_AUTH_TOKEN")
    if not value:
        login = Path.home() / ".comate" / "login"
        if login.is_file():
            value = login.read_text(encoding="utf-8").strip()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("IPIPE_AUTH_REQUIRED")
    clean = value.strip()
    return clean if clean.startswith("Bearer-") else f"Bearer-{clean}"


def _urllib_sender(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout: float,
    body_limit: int,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": response.status, "body": _read_bounded(response, body_limit)}
    except urllib.error.HTTPError as error:
        raw = _read_bounded(error, body_limit)
        raise IpipeTransportError(_http_reason(error.code), status=error.code, body=raw, transient=error.code == 429 or error.code >= 500) from None


def _read_bounded(stream: Any, body_limit: int) -> str:
    raw = stream.read(body_limit + 1)
    if len(raw) > body_limit:
        raise IpipeTransportError("IPIPE_RESPONSE_TOO_LARGE")
    return raw.decode("utf-8", errors="replace")


def _entity(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("entities"), dict):
        return value["entities"]
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    if isinstance(value, dict):
        return value
    raise IpipeTransportError("IPIPE_IDENTITY_MISSING")


def _entities(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("entities", value.get("data", []))
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise IpipeTransportError("IPIPE_RESPONSE_INVALID")
    return value


def _body_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _http_reason(status: int) -> str:
    if status == 401:
        return "AUTH_REQUIRED"
    if status == 403:
        return "PERMISSION_DENIED"
    if status == 404:
        return "OBJECT_NOT_FOUND"
    if status == 429 or status >= 500:
        return "PIPELINE_TRANSIENT"
    return "IPIPE_REQUEST_REJECTED"


def _business_reason(code: Any) -> str:
    if code in {100, 401, "100", "401"}:
        return "AUTH_REQUIRED"
    if code in {101, 403, "101", "403"}:
        return "PERMISSION_DENIED"
    if code in {404, 304, "404", "304"}:
        return "OBJECT_NOT_FOUND"
    return "IPIPE_BUSINESS_FAILURE"
