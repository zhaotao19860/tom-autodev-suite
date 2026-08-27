from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from cli_transport import CliTransport, CliTransportError
from clients.ku_client import DEFAULT_KU_BINARY
from run_summary import redact_structured


COMPONENTS = ("icafe", "ku", "icode", "review", "infoflow", "ipipe")
_PREFLIGHT_REASON_CODES = frozenset({
    "OK",
    "PREFLIGHT_QUERY_FAILED",
    "PREFLIGHT_RESPONSE_INVALID",
    "CLI_TIMEOUT",
    "CLI_NOT_FOUND",
    "CLI_INVALID_JSON",
    "CLI_BUSINESS_FAILURE",
    "CLI_PROCESS_FAILED",
    "AUTH_REQUIRED",
    "PERMISSION_DENIED",
    "OBJECT_NOT_FOUND",
    "INVALID_INPUT",
    "PROJECT_NOT_READY",
    "ICAFE_QUERY_FAILED",
    "KU_QUERY_FAILED",
    "ICODE_SKILL_NOT_FOUND",
    "ICODE_CLI_NOT_FOUND",
    "ICODE_QUERY_FAILED",
    "ICODE_LOGIN_REQUIRED",
    "ICODE_LOGIN_STATUS_UNSAFE",
    "REVIEW_COMPONENT_NOT_FOUND",
    "REVIEW_QUERY_FAILED",
    "INFOFLOW_QUERY_FAILED",
    "IPIPE_CURRENT_USER_REQUIRED",
    "IPIPE_QUERY_FAILED",
    "PIPELINE_IDENTITY_MISMATCH",
})


def run_preflight(
    project: str,
    profile: dict[str, Any],
    profile_hash: str,
    probes: dict[str, Any],
) -> dict[str, Any]:
    missing = [f"preflight.{name}" for name in COMPONENTS if not callable(getattr(probes.get(name), "query", None))]
    if missing:
        return {
            "ready": False,
            "status": "PROJECT_NOT_READY",
            "reason_code": "PROJECT_NOT_READY",
            "project": project,
            "missing": missing,
            "components": {},
        }
    contexts = _contexts(profile)
    results: dict[str, dict[str, Any]] = {}
    for name in COMPONENTS:
        try:
            raw = probes[name].query(contexts[name])
        except Exception as error:
            raw = {
                "ok": False,
                "reason_code": _exception_reason(error),
                "error_type": type(error).__name__,
            }
        results[name] = _component_result(raw)
    ready = all(result["ok"] for result in results.values())
    return {
        "ready": ready,
        "status": "READY" if ready else "PREFLIGHT_FAILED",
        "reason_code": "READY" if ready else "PREFLIGHT_FAILED",
        "project": project,
        "profile_hash": profile_hash,
        "components": results,
    }


def live_probes() -> dict[str, Any]:
    """Create adapters whose command surface contains read operations only."""
    transport = CliTransport(timeout_seconds=30)
    return {
        "icafe": _IcafeProbe(transport),
        "ku": _KuProbe(transport),
        "icode": _IcodeProbe(),
        "review": _ReviewProbe(),
        "infoflow": _InfoflowProbe(transport),
        "ipipe": _IpipeProbe(),
    }


def _contexts(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ku = next(
        (item for item in profile["knowledge_sources"] if item.get("provider") == "ku"),
        {},
    )
    repositories = [
        {key: repo[key] for key in ("path", "module", "branch")}
        for repo in [*profile["business_repos"], profile["test_repo"]]
    ]
    primary_module = profile["business_repos"][0]["module"]
    return {
        "icafe": {"project_id": profile["project_id"]},
        "ku": {"repo_id": ku.get("repo_id"), "parent_doc_id": ku.get("parent_doc_id")},
        "icode": {"repositories": repositories},
        "review": dict(profile["review_provider"]),
        "infoflow": {"channel": profile["approval_channels"]["infoflow"]["channel"]},
        "ipipe": {
            "pipeline_id": profile["pipeline_profile"]["pipeline_id"],
            "module": primary_module,
        },
    }


def _component_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return {"ok": False, "reason_code": "PREFLIGHT_RESPONSE_INVALID"}
    reason = _stable_reason(value.get("reason_code"), value["ok"])
    clean = _redact(value)
    clean["ok"] = value["ok"]
    clean["reason_code"] = reason
    return clean


def _redact(value: Any) -> Any:
    return redact_structured(value)


def _exception_reason(error: Exception) -> str:
    if isinstance(error, CliTransportError):
        return _stable_reason(error.reason_code, False)
    reason = getattr(error, "reason_code", None)
    return _stable_reason(reason, False)


def _stable_reason(value: Any, ok: bool) -> str:
    if isinstance(value, str) and value in _PREFLIGHT_REASON_CODES:
        return value
    return "OK" if ok else "PREFLIGHT_QUERY_FAILED"


class _IcafeProbe:
    def __init__(self, transport: CliTransport):
        self.transport = transport

    def query(self, _context: dict[str, Any]) -> dict[str, Any]:
        self.transport.run(["icafe-cli", "version"], expect_json=False)
        self.transport.run(["icafe-cli", "login", "status"], expect_json=False)
        return {"ok": True, "reason_code": "OK", "checks": ["version", "login-status"]}


class _KuProbe:
    def __init__(self, transport: CliTransport):
        self.transport = transport

    def query(self, context: dict[str, Any]) -> dict[str, Any]:
        self.transport.run(
            [
                DEFAULT_KU_BINARY, "query-repo", "--repo-id", context["repo_id"],
                "--parent-doc-id", context["parent_doc_id"], "--page-num", "1", "--page-size", "1",
            ],
            business_field="returnCode",
        )
        return {"ok": True, "reason_code": "OK", "target_access": True}


class _IcodeProbe:
    def __init__(
        self,
        *,
        skill_path: Path | str | None = None,
        binary_resolver: Any = shutil.which,
        transport: Any | None = None,
    ):
        self.skill_path = Path(skill_path).expanduser() if skill_path is not None else (
            Path.home() / ".comate" / "skills" / ".system" / "icode"
        )
        self.binary_resolver = binary_resolver
        self.transport = transport or _ArgvQueryTransport()

    def query(self, context: dict[str, Any]) -> dict[str, Any]:
        # The iCode git CLI is `icode`; `icode-cli` is a separate code-review CLI that
        # exposes neither `login` nor a `--version` flag.
        binary = self.binary_resolver("icode")
        if not self.skill_path.is_dir():
            return {"ok": False, "reason_code": "ICODE_SKILL_NOT_FOUND"}
        if binary is None:
            return {"ok": False, "reason_code": "ICODE_CLI_NOT_FOUND"}
        version = self.transport.run([binary, "version"])
        if version.get("returncode") != 0:
            return {"ok": False, "reason_code": "ICODE_QUERY_FAILED"}
        repositories = context.get("repositories")
        if not isinstance(repositories, list) or not repositories:
            return {"ok": False, "reason_code": "PROJECT_NOT_READY"}
        login = self.transport.run(
            [binary, "login"], cwd=Path(repositories[0]["path"]), stdin_closed=True
        )
        if login.get("returncode") != 0:
            return {"ok": False, "reason_code": "ICODE_LOGIN_REQUIRED"}
        output = f"{login.get('stdout', '')}\n{login.get('stderr', '')}"
        if re.search(r"(?im)^Already logged in as:\s+\S+", output) is None:
            return {"ok": False, "reason_code": "ICODE_LOGIN_STATUS_UNSAFE"}
        return {"ok": True, "reason_code": "OK", "checks": ["version", "login"]}


class _ReviewProbe:
    def __init__(self, *, executable_resolver: Any = shutil.which):
        self.executable_resolver = executable_resolver

    def query(self, context: dict[str, Any]) -> dict[str, Any]:
        try:
            argv = shlex.split(context["command"])
        except (TypeError, ValueError):
            argv = []
        executable = argv[0] if argv else ""
        found = bool(executable) and (
            (Path(executable).expanduser().is_file() and os.access(Path(executable).expanduser(), os.X_OK))
            or self.executable_resolver(executable) is not None
        )
        return {
            "ok": found,
            "reason_code": "OK" if found else "REVIEW_COMPONENT_NOT_FOUND",
            "kind": context.get("kind"),
        }


class _InfoflowProbe:
    def __init__(self, transport: CliTransport, *, setup_path: Path | str | None = None):
        self.transport = transport
        self.setup_path = Path(setup_path).expanduser() if setup_path is not None else (
            Path.home() / ".comate" / "skills" / "infoflow-message-group" / "scripts" / "setup.sh"
        )

    def query(self, _context: dict[str, Any]) -> dict[str, Any]:
        self.transport.run([str(self.setup_path), "--check"], expect_json=False)
        return {"ok": True, "reason_code": "OK", "configuration": "present"}


class _IpipeProbe:
    def __init__(self, *, client_factory: Any | None = None, username: str | None = None):
        self.client_factory = client_factory
        self.username = username

    def query(self, context: dict[str, Any]) -> dict[str, Any]:
        from clients.ipipe_client import IpipeApiClient, IpipeHttpTransport

        username = self.username or os.environ.get("COMATE_USERNAME") or os.environ.get("BAIDU_CC_USERNAME")
        if not username:
            return {"ok": False, "reason_code": "IPIPE_CURRENT_USER_REQUIRED"}
        client = (
            self.client_factory(username)
            if self.client_factory is not None
            else IpipeApiClient(IpipeHttpTransport(), current_user=username)
        )
        # Identity is confirmed from the module's pipeline listing, not from the
        # pipeline-conf endpoint: that response carries the whole stage tree (75KB for a
        # real BGW pipeline, above the transport's bounded body limit) and does not
        # include the owning module at all.
        owned = client.pipelines_by_module(context["module"])
        for item in owned:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id") or item.get("pipelineConfId") or "")
            if identity != context["pipeline_id"]:
                continue
            module = str(item.get("moduleName") or item.get("module") or context["module"])
            if module != context["module"]:
                return {"ok": False, "reason_code": "PIPELINE_IDENTITY_MISMATCH"}
            return {"ok": True, "reason_code": "OK", "pipeline_id": identity, "module": module}
        return {"ok": False, "reason_code": "PIPELINE_IDENTITY_MISMATCH"}


class _ArgvQueryTransport:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        stdin_closed: bool = False,
    ) -> dict[str, Any]:
        import subprocess

        if argv[-1] == "login" and not stdin_closed:
            return {"returncode": 1, "stdout": "", "stderr": ""}
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            **({"stdin": subprocess.DEVNULL} if stdin_closed else {}),
            timeout=30,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[:1024],
            "stderr": (completed.stderr or "")[:1024],
        }
