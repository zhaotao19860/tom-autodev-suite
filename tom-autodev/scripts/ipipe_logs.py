"""Read what an iPipe job actually did, from its log.

A job's status is the exit code of a shell script, and a stage that runs product cases
without checking their result reports SUCC over a run where every case failed. That
happened on BGW-1956: `P0新功能调试` returned SUCC while all four case directories
reported `success_ratio: 0.00%`. Nothing in the control plane could see it, because the
only place that number exists is the log.

Two capabilities live here, in the order they are needed:

1. Fetch. The gateway hands out a log URL on an intranet host and no API for its text.
   Plain `curl` from the machine running this control plane reaches it; the web-fetch
   tooling declines intranet domains by policy, which is a tooling boundary rather than a
   network fact -- mistaking one for the other cost a whole round of "the log is
   unreadable".
2. Parse. `success_ratio`, the failed case names, and the gate comparisons the script
   itself performs are the evidence. When the log does not say enough, the log still says
   *where* the work happened: the agent host and the script path, which is exactly what
   `tom-autodebug` needs to go and look.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

_MAX_BYTES = 8 * 1024 * 1024
_HOST = re.compile(r"Running on\s+(?P<host>[\w.-]+)\((?P<ip>[\d.]+)\)")
_WORKSPACE = re.compile(r"in workspace\s+(?P<workspace>\S+)")
_SCRIPT = re.compile(r"^\s*\+?\s*(?:cd\s+(?P<cd>\S+)|(?P<script>\S*(?:script/|\.sh\b)\S*)(?P<args>[^\n]*))$")
_RATIO = re.compile(r"success_ratio[:,]\s*(?P<ratio>[\d.]+)%?")
_FAILED = re.compile(r"失败\((?P<count>\d+)\):\s*(?P<names>.+)$")
_CASE_RESULT = re.compile(r"case:\s*(?P<case>\S+);\s*res:\s*(?P<result>True|False)")
_GATE = re.compile(r"\(\(\s*(?P<actual>\d+)\s*<\s*(?P<threshold>\d+)\s*\)\)")


def fetch(
    url: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """The log's text, or the reason it could not be read."""
    if not isinstance(url, str) or not url.startswith("http"):
        return {"ok": False, "reason_code": "LOG_URL_INVALID"}
    argv = ["curl", "-sS", "--max-time", str(int(timeout_seconds)), "--max-filesize", str(_MAX_BYTES), url]
    execute = runner or (
        lambda command: subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_seconds + 30, check=False
        )
    )
    try:
        completed = execute(argv)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "reason_code": "LOG_FETCH_FAILED", "url": url}
    if completed.returncode != 0:
        return {
            "ok": False,
            "reason_code": "LOG_FETCH_FAILED",
            "url": url,
            "detail": (completed.stderr or "")[:200],
        }
    text = completed.stdout or ""
    if not text.strip():
        return {"ok": False, "reason_code": "LOG_EMPTY", "url": url}
    return {"ok": True, "reason_code": "OK", "url": url, "text": text}


def parse(text: str) -> dict[str, Any]:
    """The case results, gate comparisons, and where the job ran."""
    if not isinstance(text, str):
        return {"ok": False, "reason_code": "LOG_TEXT_INVALID"}
    lines = text.splitlines()
    host = ip = workspace = None
    directories: list[str] = []
    ratios: list[float] = []
    failed: list[str] = []
    cases: list[dict[str, Any]] = []
    gates: list[dict[str, int]] = []
    scripts: list[str] = []
    for line in lines:
        found_host = _HOST.search(line)
        if found_host and host is None:
            host, ip = found_host.group("host"), found_host.group("ip")
        found_workspace = _WORKSPACE.search(line)
        if found_workspace and workspace is None:
            workspace = found_workspace.group("workspace")
        for match in _SCRIPT.finditer(_command_of(line)):
            candidate = match.group("script") or match.group("cd")
            if candidate and candidate not in scripts:
                scripts.append(candidate)
        found_ratio = _RATIO.search(line)
        if found_ratio:
            ratios.append(float(found_ratio.group("ratio")))
        found_failed = _FAILED.search(line)
        if found_failed:
            failed.extend(_names(found_failed.group("names")))
        found_case = _CASE_RESULT.search(line)
        if found_case:
            cases.append({"case": found_case.group("case"), "passed": found_case.group("result") == "True"})
        found_gate = _GATE.search(line)
        if found_gate:
            gates.append({
                "actual": int(found_gate.group("actual")),
                "threshold": int(found_gate.group("threshold")),
            })
        for directory in re.findall(r"(case_[\w]+)/res\.csv", line):
            if directory not in directories:
                directories.append(directory)
    return {
        "ok": True,
        "reason_code": "OK",
        "agent_host": host,
        "agent_ip": ip,
        "workspace": workspace,
        "scripts": scripts[:10],
        "case_directories": directories,
        "success_ratios": ratios,
        "failed_cases": sorted(set(failed)),
        "case_results": cases,
        "gates": gates,
    }


def _command_of(line: str) -> str:
    """The command part of a timestamped log line."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*", "", line)


def _names(value: str) -> list[str]:
    cleaned = re.sub(r"\.\.\.\(共\s*\d+\s*个\)", " ", value)
    return [item for item in re.split(r"[\s,]+", cleaned) if item.startswith("test_")]
