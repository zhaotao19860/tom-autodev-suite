"""Watch a run's iPipe build and say something the moment it needs a person.

`IpipeRuntime.monitor` already classifies a build, but it only runs while the agent
happens to be driving the IPIPE phase. A build that fails, or parks on a manual
stage, at 02:00 sat unreported until someone opened the CLI. This watcher polls it
in the background and routes the three outcomes into 如流.

It never advances a phase and never edits code. A failure is reported with its
signature, classification and the repair direction that follows from it; turning
that into a change is DIAGNOSE's job, which needs the agent. A background process
deciding engineering on its own is the one thing this must not do.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from approval_delivery import deliver_markdown
from approval_watch import _ide_turn_recipients

_NOTICE_KEY = "ipipe-watch-notice"
_NUDGE_KEY = "ipipe-watch-nudge"
_NUDGE_SECONDS = 1800
_TERMINAL_STATES = frozenset({"RELEASE_SUCCESS", "STOPPED"})
# What a failure means for whoever gets woken up, keyed by the runtime's own
# classification so the two never drift into disagreeing.
_REPAIR_DIRECTION = {
    "CODE_FAILURE": "研发修代码：回 DIAGNOSE 生成修复方案，再走 PLAN/IMPLEMENT",
    "TEST_FAILURE": "先判定是用例问题还是产品缺陷：用例问题由测试改用例，缺陷回 DIAGNOSE",
    "ENVIRONMENT_FAILURE": "环境问题：不要改代码，确认 runner/镜像/容量后用 G8 重跑该阶段",
    "MIXED_FAILURE": "多个阶段同时失败，先按最早失败的阶段定性，再决定是否回 DIAGNOSE",
    "PIPELINE_FAILURE": "流水线自身失败，先看阶段日志确认是否为平台问题",
}


class IpipeWatcher:
    """Poll the run's build, report SUCCESS / FAILURE / MANUAL_WAIT once each."""

    def __init__(
        self,
        orchestrator: Any,
        runtime_factory: Any,
        notify_client: Any,
        *,
        sleeper: Any | None = None,
        reporter: Any | None = None,
        clock: Any | None = None,
        window_seconds: int = 120,
    ):
        self.orchestrator = orchestrator
        self.runtime_factory = runtime_factory
        self.notify_client = notify_client
        self.sleeper = sleeper or time.sleep
        self.reporter = reporter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.window_seconds = window_seconds

    def run(self, interval: float, iterations: int | None = None) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        count = 0
        while iterations is None or count < iterations:
            settled.extend(self.tick())
            count += 1
            if iterations is None or count < iterations:
                self.sleeper(interval)
        return settled

    def tick(self) -> list[dict[str, Any]]:
        outcomes = []
        for latest in self.orchestrator.state.latest_states():
            if latest.get("state") != "IPIPE":
                continue
            outcome = self._observe(latest["run_id"])
            if outcome is None:
                continue
            outcomes.append(outcome)
            if self.reporter is not None:
                self.reporter(outcome)
        return outcomes

    def _observe(self, run_id: str) -> dict[str, Any] | None:
        build_id = self._build_id(run_id)
        if build_id is None:
            return {"ok": True, "reason_code": "NO_BUILD_YET", "run_id": run_id}
        runtime = self.runtime_factory(run_id)
        if isinstance(runtime, dict):
            return {**runtime, "run_id": run_id}
        deadline = (self.clock() + timedelta(seconds=self.window_seconds)).isoformat()
        try:
            result = runtime.monitor(build_id, deadline)
        except Exception as error:  # noqa: BLE001 - a poll must not kill the watcher
            return {"ok": False, "reason_code": "MONITOR_CALL_FAILED", "run_id": run_id,
                    "detail": str(error)}
        status = result.get("status")
        if status == "SUCCESS":
            return self._notice(run_id, f"success:{build_id}", _success_markdown, result)
        if status == "FAILURE":
            token = f"failure:{build_id}:{result.get('failure_signature') or ''}"
            return self._notice(run_id, token, _failure_markdown, result)
        if status == "MANUAL_WAIT":
            return self._manual(run_id, result)
        # TIMEOUT and the transport refusals are transient by construction: the next
        # tick asks again, and a notice per poll would train everyone to ignore them.
        return {"ok": True, "reason_code": f"NO_NOTICE_{status or 'UNKNOWN'}", "run_id": run_id}

    def _build_id(self, run_id: str) -> str | None:
        """The build this run triggered, from the trigger receipt rather than a guess."""
        found = None
        for item in self.orchestrator.state.external_results(run_id):
            if item.get("intent", {}).get("operation") != "ipipe.trigger":
                continue
            response = item.get("receipt", {}).get("response")
            if isinstance(response, dict) and response.get("build_id"):
                found = str(response["build_id"])
        return found

    def _notice(self, run_id: str, token: str, render: Any, result: dict[str, Any]) -> dict[str, Any]:
        key = f"{_NOTICE_KEY}:{run_id}:{token}"
        stored = self.orchestrator.state.idempotency_result(key)
        if stored is not None:
            return {"ok": True, "reason_code": "ALREADY_NOTIFIED", "run_id": run_id,
                    "first_at": stored.get("at")}
        recipients = sorted(_ide_turn_recipients(self.orchestrator, run_id))
        if not recipients:
            return {"ok": True, "reason_code": "NO_RECIPIENTS", "run_id": run_id}
        at = self.clock().isoformat()
        try:
            receipt = deliver_markdown(
                self.notify_client, self.orchestrator.state, run_id, recipients,
                lambda mention: render(run_id, result, recipients if mention else None),
            )
        except Exception as error:  # noqa: BLE001 - a missed notice must not stop the watch
            return {"ok": False, "reason_code": "IPIPE_NOTICE_FAILED", "run_id": run_id,
                    "detail": str(error)}
        self.orchestrator.state.save_idempotency_result(key, {"receipt": receipt, "at": at})
        return {"ok": True, "reason_code": "OK", "run_id": run_id, "status": result.get("status"),
                "receipt": receipt}

    def _manual(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """One notice when the stage opens, then a private nudge every half hour.

        The anchor is the first notice's own timestamp: iPipe does not report when a
        stage started waiting, and inventing a start time would make the reminder
        interval a lie. The bucket index keeps each nudge idempotent without storing
        a counter.
        """
        stage = result.get("stage_build_id") or ""
        token = f"manual:{result.get('build_id')}:{stage}"
        first = self._notice(run_id, token, _manual_markdown, result)
        if first.get("reason_code") != "ALREADY_NOTIFIED":
            return first
        anchor = _parsed(first.get("first_at"))
        if anchor is None:
            return first
        elapsed = (self.clock() - anchor).total_seconds()
        bucket = int(elapsed // _NUDGE_SECONDS)
        if bucket < 1:
            return {"ok": True, "reason_code": "MANUAL_WAIT_TOO_EARLY", "run_id": run_id}
        key = f"{_NUDGE_KEY}:{run_id}:{token}:{bucket}"
        if self.orchestrator.state.idempotency_result(key) is not None:
            return {"ok": True, "reason_code": "NUDGE_ALREADY_SENT", "run_id": run_id}
        recipients = sorted(_ide_turn_recipients(self.orchestrator, run_id))
        if not recipients:
            return {"ok": True, "reason_code": "NO_RECIPIENTS", "run_id": run_id}
        try:
            # Single chat on purpose: the group already carries the first notice, and a
            # reminder is addressed at one person's inbox, not at everyone again.
            receipt = self.notify_client.send_markdown(
                recipients, _nudge_markdown(run_id, result, int(elapsed // 60))
            )
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "reason_code": "IPIPE_NUDGE_FAILED", "run_id": run_id,
                    "detail": str(error)}
        self.orchestrator.state.save_idempotency_result(key, {"receipt": receipt})
        return {"ok": True, "reason_code": "NUDGED", "run_id": run_id, "minutes": int(elapsed // 60)}


def _parsed(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _head(brief: dict[str, Any], run_id: str, mention: Any) -> list[str]:
    lines = [brief["title"], ""]
    if mention:
        from approval_delivery import mention_line

        lines += [f"**待操作** {mention_line(list(mention))}", ""]
    lines += [f"**run_id** `{run_id[:12]}…`", f"**构建** {brief['build_id']}"]
    return lines


def _stage_summary(result: dict[str, Any]) -> str:
    stages = result.get("stages") or []
    return "、".join(
        f"{stage.get('name') or stage.get('stage_build_id')}={stage.get('status')}"
        for stage in stages
    ) or "-"


def _success_markdown(run_id: str, result: dict[str, Any], mention: Any = None) -> str:
    lines = _head({"title": "## iPipe 通过，等你在 IDE 继续", "build_id": result.get("build_id")},
                  run_id, mention)
    lines += [
        f"**各阶段** {_stage_summary(result)}",
        "",
        "**请在 Comate 里回复**",
        "继续",
        "",
        "> 流水线证据已可落账，推进阶段需要 Comate 执行。",
    ]
    return "\n".join(lines)


def _failure_markdown(run_id: str, result: dict[str, Any], mention: Any = None) -> str:
    classification = str(result.get("classification") or "PIPELINE_FAILURE")
    excerpt = str(result.get("log_excerpt") or "").strip()
    lines = _head({"title": "## iPipe 失败，需要人工介入", "build_id": result.get("build_id")},
                  run_id, mention)
    lines += [
        f"**定性** {classification}",
        f"**失败签名** `{result.get('failure_signature') or '-'}`",
        f"**各阶段** {_stage_summary(result)}",
        f"**修复方向** {_REPAIR_DIRECTION.get(classification, _REPAIR_DIRECTION['PIPELINE_FAILURE'])}",
    ]
    if excerpt:
        lines += ["", "**日志摘录**", "```", excerpt[:800], "```"]
    lines += [
        "",
        "**请在 Comate 里回复**",
        "继续",
        "",
        "> 修复方案由 DIAGNOSE 阶段产出，后台进程不会自己改代码。",
    ]
    return "\n".join(lines)


def _manual_markdown(run_id: str, result: dict[str, Any], mention: Any = None) -> str:
    lines = _head({"title": "## iPipe 停在人工阶段，等人操作", "build_id": result.get("build_id")},
                  run_id, mention)
    lines += [
        f"**人工阶段** {result.get('stage_build_id')}",
        f"**各阶段** {_stage_summary(result)}",
        "",
        "> 这一步要在 iPipe 上点，流程会一直等着。之后每半小时私聊提醒一次。",
    ]
    return "\n".join(lines)


def _nudge_markdown(run_id: str, result: dict[str, Any], minutes: int) -> str:
    return "\n".join([
        "## iPipe 人工阶段还在等",
        "",
        f"**已等** {minutes} 分钟",
        f"**构建** {result.get('build_id')}",
        f"**人工阶段** {result.get('stage_build_id')}",
        f"**run_id** `{run_id[:12]}…`",
        "",
        "> 在 iPipe 上完成该阶段后，流程才会继续。",
    ])
