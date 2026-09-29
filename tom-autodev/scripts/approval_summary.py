"""What a gate is actually asking a person to approve.

An approval message that only carries an id and a deadline asks for a signature on
an unnamed thing: the approver cannot tell a requirement clarification from a
release, and the `input_hash` binding is worthless if nobody knows what it binds.
Every gate therefore states its subject, what approving it causes, and a summary
of the artifact derived from the very content the hash covers.
"""
from __future__ import annotations

import re
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import workflow_spec

# Subject and effect per gate, from the single workflow spec (workflow_spec.py).
_GATES: dict[str, tuple[str, str]] = workflow_spec.GATE_LABELS


# Presentation only: do not change the pinned workflow fingerprint for copy edits.
_REVIEW = {
    "G0": ("需求范围、验收与项目；群名、成员、角色；change_class 及免审阶段",
           "需求快照、启动事件中的 profile_path/profile_hash、协作绑定和路由"),
    "G1": ("逐项决策、备选方案、未决问题与新增验收点",
           "Decision Log 的 decisions / acceptance_delta / unresolved_frontier 与来源证据"),
    "G2": ("行为、非目标、验收场景、测试接口、环境；合并模式还要审任务覆盖",
           "Spec 全文；合并草案中的 dag 为关联设计，G2 hash 只绑定 Spec"),
    "G3": ("端到端切片、依赖无环、业务与独立测试仓、验收覆盖无遗漏",
           "Task DAG 的 nodes / edges / acceptance_coverage"),
    "G4": ("先区分工作区绑定与 Task Plan；仓库、分支、固定修订、文件、测试和回滚",
           "WORKSPACE receipts/binding 或当前任务 Task Plan；不可互相替代"),
    "G5": ("业务与测试完整 diff、基线/候选修订、验收追溯及每一项计划偏离",
           "Change Set 的 business_patch / test_patch / full_diff_hash / deviations；不是只看摘要"),
    "G6": ("失败证据、根因、修复范围、预算与回退路径",
           "Failure Bundle、Diagnosis 的 repair_scope / proposed_changes / evidence_refs"),
    "G7": ("区分 iCode 提交与 iPipe 首次触发；精确版本、目标分支、模块和参数",
           "已归档 submit descriptor + Review，或当前模块 trigger binding；两者 hash 不通用"),
    "G8": ("失败重跑或人工阶段继续；build/stage、真实失败原因及脱敏参数",
           "当前阶段远程证据、case_failures、rerun binding 与参数来源"),
    "G9": ("全部必需模块的 build、版本、环境、发布规则和发布证据一致",
           "冻结 pipeline plan 与本次 action 钉住的全部 iPipe 产物；旧 build 不可替代"),
    "G10": ("控制面候选 diff、允许目录、收益、风险、验证命令与回滚",
            "Run Summary、Optimization Proposal、candidate.target_files 与 before_sha256"),
    "PROFILE_REPIN": ("旧新 profile 差异、允许字段、既有提交绑定保持有效",
                      "已校验旧副本与当前 profile；old_hash / new_hash / changed_keys"),
}
_EFFECTS = {
    "G0": "批准当前需求/协作/路由绑定；控制器随后建群并进入 GRILL，不授权代码提交或发布",
    "G2": "发布 Spec；按当前 change_class 处理 TASKS（合并路由不另开 G3），再进入 WORKSPACE",
    "G3": "发布 DAG，进入 WORKSPACE 准备绑定；尚未批准 Task Plan",
    "G4": "WORKSPACE 绑定获批仅进入 PLAN；Task Plan 获批才进入 IMPLEMENT（以本次对象为准）",
    "G6": "按诊断路由回 PLAN / SPEC / ARCHITECTURE_REVIEW 或 STOPPED；后续代码/提交门仍须分别通过",
    "G7": "仅执行本次 hash 绑定的 iCode 提交或单模块 iPipe 触发；不是两种操作的通用授权",
    "G8": "仅重跑或继续本次 build/stage 和参数绑定的阶段，不批准新代码或发布",
    "G9": "只读核验平台发布并记录匹配证据；全部模块通过才进入 RELEASE_SUCCESS，未发布则等待，不强制发布",
    "G10": "仅应用允许目录内的控制面候选改动并验证/失败回滚；不修改业务 run 的授权",
    "PROFILE_REPIN": "执行另行 apply 后移动 profile pin；不改阶段、不重新授权既有提交，不允许改变审批成员",
}


def gate_intent(action: Any) -> dict[str, Any]:
    from approval_ledger import gate_of
    name = gate_of(str(action))
    subject, effect = _GATES.get(name, ("", ""))
    if name == "PROFILE_REPIN":
        subject = "将运行重新钉到已审核的 project profile"
    focus, reads = _REVIEW.get(name, ("核对本次动作与绑定", "缺少已知审批合同，请先补齐"))
    return {"subject": subject, "effect": _EFFECTS.get(name, effect),
            "checklist": [focus, reads],
            "decision": "同意仅授权本次 input_hash；驳回不放行。内容变化须重新审批，审批本身不推进阶段。"}


def references(value: Any, prefix: str = "") -> list[dict[str, str]]:
    """Extract actual locators/identities, never synthesize paths or remote URLs.

    Inline patches stay in their pinned document; they are not file names. This
    allowlist also avoids copying arbitrary parameter/credential values.
    """
    scalar_keys = {
        "path", "repo_path", "worktree_path", "profile_path", "knowledge_url",
        "artifact_id", "summary_artifact_id", "reviewed_artifact_id", "build_id",
        "stage_build_id", "pipeline_id", "module", "branch", "target_branch",
        "commit_revision", "revision", "release_rule", "environment_fingerprint",
        "plan_hash", "binding_hash", "content_hash", "full_diff_hash", "candidate_hash",
        "old_hash", "new_hash", "before_sha256",
    }
    list_keys = {"files", "evidence_refs", "source_evidence_refs", "evidence_links"}
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            label = f"{prefix}.{key}" if prefix else key
            if key in scalar_keys and isinstance(item, str) and item:
                result.append({"label": label, "ref": item})
            elif key in list_keys and isinstance(item, list):
                result.extend({"label": label, "ref": ref} for ref in item if isinstance(ref, str))
            elif key in {"business_patch", "test_patch"} and isinstance(item, str):
                result.append({"label": label, "ref": "内嵌完整 patch，见绑定草案/产物该字段"
                               if "\n" in item or item.startswith("diff ") else item})
            elif key in {"baseline_revisions", "revisions", "source_revisions"} and isinstance(item, dict):
                result.extend({"label": f"{label}.{role}", "ref": revision}
                              for role, revision in item.items() if isinstance(revision, str))
            elif isinstance(item, (dict, list)) and key not in {"parameters", "ipipe_parameters", "content"}:
                result.extend(references(item, label))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(references(item, f"{prefix}[{index}]"))
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _missing() -> dict[str, Any]:
    return {
        "summary": ["未取得与本次 hash 匹配的内容；请先补齐材料，不要仅凭标题批准。"],
        "references": [],
        "material_status": "MISSING",
    }


def _runtime_profile(orchestrator: Any, run_id: str) -> dict[str, Any] | None:
    getter = getattr(orchestrator, "_runtime_profile", None)
    if not callable(getter):
        return None
    result = getter(run_id)
    return result.get("profile") if isinstance(result, dict) and result.get("ok") else None


def _g0_context(orchestrator: Any, run_id: str, input_hash: str) -> dict[str, Any]:
    events = orchestrator.state.events(run_id)
    payload = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
    if payload.get("g0_input_hash") != input_hash:
        return _missing()
    profile = _runtime_profile(orchestrator, run_id) or {}
    binding = payload.get("collaboration_binding")
    binding = binding if isinstance(binding, dict) else {}
    material = {
        "requirement_id": payload.get("requirement_id"),
        "project": payload.get("project"),
        "profile_path": payload.get("profile_path"),
        "profile_hash": payload.get("profile_hash"),
        "workflow_modes": payload.get("workflow_modes"),
        "collaboration_binding": binding,
    }
    refs = references(material)
    refs += references({"profile": profile})
    refs = list(dict((item["ref"], item) for item in refs).values())
    return {
        "summary": [
            f"需求 {payload.get('requirement_id') or '-'} / 项目 {payload.get('project') or '-'}",
            f"profile {payload.get('profile_path') or '-'}",
            f"协作成员 {len(binding.get('member_snapshot') or [])} 人",
        ],
        "references": refs,
        "content_hash": _hash(material),
        "material_status": "PINNED",
        "_review_content": material,
    }


def _workspace_context(orchestrator: Any, run_id: str, input_hash: str) -> dict[str, Any]:
    """Reconstruct G4 from active ownership using read-only queries only."""
    try:
        from run_brief import _read_only_next
        from workspace_manager import _ownership_database

        action = _read_only_next(orchestrator, run_id)
        if not action.get("ok") or action.get("phase") != "WORKSPACE":
            return _missing()
        task_id = action.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return _missing()
        profile = _runtime_profile(orchestrator, run_id)
        if not isinstance(profile, dict):
            return _missing()
        db = _ownership_database(orchestrator.workspaces.worktree_root)
        if not db.exists():
            return _missing()
        uri = f"file:{db}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM worktree_ownership WHERE run_id = ? AND status = 'ACTIVE'",
                (run_id,),
            ).fetchall()
        repositories = {}
        business = profile.get("business_repos") if isinstance(profile.get("business_repos"), list) else []
        test_repo = profile.get("test_repo") if isinstance(profile.get("test_repo"), dict) else {}
        for row in rows:
            repo_path = str(row["repo_path"])
            role = None
            module = None
            for candidate in business:
                if isinstance(candidate, dict) and str(Path(candidate.get("path", "")).expanduser().resolve()) == repo_path:
                    role, module = "business", candidate.get("module")
                    break
            if role is None and str(Path(test_repo.get("path", "")).expanduser().resolve()) == repo_path:
                role, module = "tests", test_repo.get("module")
            if role is None or not module or role in repositories:
                continue
            verify = orchestrator.workspaces.query_ownership(
                repo_path, run_id, task_id, str(row["owner_token"])
            )
            if (
                verify.get("status") != "VERIFIED"
                or verify.get("ownership_status") != "ACTIVE"
                or verify.get("run_id") != run_id
                or verify.get("task_id") != task_id
            ):
                return _missing()
            repositories[role] = {
                "role": role,
                "module": module,
                "repo_path": repo_path,
                "worktree_path": str(row["worktree_path"]),
                "baseline_revision": str(row["baseline_revision"]),
                "owner_proof": hashlib.sha256(str(row["owner_token"]).encode()).hexdigest(),
            }
        if set(repositories) != {"business", "tests"}:
            return _missing()
        events = orchestrator.state.events(run_id)
        intake = events[0].get("payload") if events and isinstance(events[0].get("payload"), dict) else {}
        binding = {
            "schema_version": "1",
            "run_id": run_id,
            "source_event_id": action.get("source_event_id"),
            "task_id": task_id,
            "profile_hash": intake.get("profile_hash"),
            "repositories": repositories,
            "source_revisions": {
                "business": repositories["business"]["baseline_revision"],
                "tests": repositories["tests"]["baseline_revision"],
            },
        }
        if _hash({"gate": "G4", "workspace_binding": binding}) != input_hash:
            return _missing()
        return {
            "summary": [
                f"任务 {task_id}",
                f"业务仓库 {repositories['business']['module']} @ {repositories['business']['baseline_revision']}",
                f"测试仓库 {repositories['tests']['module']} @ {repositories['tests']['baseline_revision']}",
            ],
            "references": references(binding),
            "content_hash": input_hash,
            "material_status": "PINNED",
            "_review_content": {"gate": "G4", "workspace_binding": binding},
        }
    except (AttributeError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        return _missing()


def pinned_context(orchestrator: Any, run_id: str, gate_name: str, input_hash: str) -> dict[str, Any]:
    """Read only the exact current action/draft or hash-matching archived descriptor.

    Never invoke a producer, build_and_archive, runtime transport, or a latest-file
    fallback. Failure is visible and does not pretend the missing content was read.
    """
    from run_brief import _read_only_next
    from worker_driver import build_envelope

    missing = _missing()
    try:
        if gate_name == "G0":
            return _g0_context(orchestrator, run_id, input_hash)
        if gate_name == "G4":
            return _workspace_context(orchestrator, run_id, input_hash)
        if gate_name == "G10":
            proposal_reader = getattr(orchestrator.state, "optimization_proposal", None)
            if callable(proposal_reader):
                # The proposal id is derived from the run and candidate hash. A
                # state adapter may accept the candidate hash directly, so both
                # forms are attempted without selecting a latest proposal.
                from run_summary import _proposal_id
                stored = proposal_reader(_proposal_id(run_id, input_hash))
                proposal = stored.get("proposal") if isinstance(stored, dict) else None
                proposal = proposal if isinstance(proposal, dict) else stored
                candidate = proposal.get("candidate") if isinstance(proposal, dict) else None
                if isinstance(candidate, dict) and _hash(candidate) == input_hash:
                    return {
                        "summary": content_summary(proposal),
                        "references": references(proposal),
                        "content_hash": _hash(proposal),
                        "material_status": "PINNED",
                        "_review_content": proposal,
                    }
        action = _read_only_next(orchestrator, run_id)
        if gate_name != "G9" and action.get("ok") and action.get("required_human_gate") == gate_name:
            job_id = f"producer:{action.get('action_id')}"
            job = orchestrator.state.producer_job(job_id)
            draft = job.get("draft") if isinstance(job, dict) and job.get("status") == "FULFILLED" else None
            if isinstance(draft, dict):
                merged = action.get("phase") == "SPEC" and isinstance(draft.get("spec"), dict)
                content = draft["spec"] if merged else draft
                envelope = build_envelope(action, content)
                if envelope["approval_input_hash"] == input_hash:
                    refs = [{"label": "已保存草案（本地账本，不是网页）", "ref": job_id}]
                    refs += references(content) + references(action)
                    summary = content_summary(content)
                    if merged:
                        summary += ["关联 DAG 随草案保存；G2 只绑定 Spec，DAG 不构成独立 G3 批准。"]
                        refs += references(draft.get("dag"), "关联dag")
                    return {"summary": summary, "references": refs,
                            "content_hash": envelope["content_hash"], "material_status": "PINNED",
                            "_review_content": {"draft": draft}}
            if action.get("input_hash") == input_hash:
                refs = references(action)
                summary = [f"当前动作 {action.get('phase')}；任务 {action.get('task_id') or '-'}"]
                for ref in action.get("input_artifacts") or []:
                    artifact = orchestrator.artifacts.get(ref["artifact_id"])
                    if not artifact.get("valid") or artifact.get("run_id") != run_id:
                        return missing
                    envelope = json.loads(artifact["content"])
                    content = envelope.get("content")
                    if not isinstance(content, dict) or _hash(content) != ref.get("content_hash"):
                        return missing
                    refs += [{"label": "固定上游产物", "ref": artifact["path"]}]
                    refs += references(envelope) + references(content)
                    summary += content_summary(content)
                return {"summary": summary, "references": refs, "material_status": "PINNED",
                        "_review_content": {"artifacts": refs}}
        if gate_name == "G9" and action.get("ok") and action.get("required_human_gate") == gate_name:
            if action.get("input_hash") != input_hash:
                return missing
            events = orchestrator.state.events(run_id)
            plan = next(
                (
                    (event.get("payload") or {}).get("pipeline_plan")
                    for event in reversed(events)
                    if isinstance((event.get("payload") or {}).get("pipeline_plan"), dict)
                ),
                None,
            )
            refs = references(action)
            contents = []
            for ref in action.get("input_artifacts") or []:
                artifact = orchestrator.artifacts.get(ref.get("artifact_id"))
                if not artifact.get("valid") or artifact.get("run_id") != run_id:
                    return missing
                envelope = json.loads(artifact["content"])
                content = envelope.get("content")
                if not isinstance(content, dict) or _hash(content) != ref.get("content_hash"):
                    return missing
                refs += [{"label": "固定 iPipe 产物", "ref": artifact["path"]}]
                refs += references(content)
                contents.append(content)
            if not isinstance(plan, dict) or not contents:
                return missing
            return {
                "summary": content_summary(plan) + [line for content in contents for line in content_summary(content)],
                "references": refs,
                "content_hash": _hash({"pipeline_plan": plan, "artifacts": contents}),
                "material_status": "PINNED",
                "_review_content": {"pipeline_plan": plan, "artifacts": contents},
            }
        # Submission and G10 are not producer envelopes. Match the exact canonical
        # object hash rather than selecting the latest artifact for a phase.
        if gate_name in {"G7", "G10"}:
            for artifact in orchestrator.artifacts.artifacts_for_run(run_id):
                if not artifact.get("valid"):
                    continue
                content = json.loads(artifact["content"])
                candidate = content.get("candidate") if gate_name == "G10" else content
                if isinstance(candidate, dict) and _hash(candidate) == input_hash:
                    return {"summary": content_summary(content),
                            "references": [{"label": "固定审批产物", "ref": artifact["path"]}] + references(content),
                            "content_hash": _hash(content), "material_status": "PINNED"}
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return missing
    return missing


def content_summary(content: Any) -> list[str]:
    """Summarize an artifact by its own shape, never by guessing at a schema.

    Dispatch is on the fields the schemas actually define, so an unrecognized
    artifact degrades to a field listing instead of inventing labels.
    """
    if not isinstance(content, dict):
        return []
    if "decision_result" in content:
        return _decision_log(content)
    if "behaviors" in content and "acceptance_scenarios" in content:
        return _spec(content)
    if "nodes" in content and "edges" in content:
        return _task_dag(content)
    if "plan_id" in content:
        return _task_plan(content)
    if _is_submit_descriptor(content):
        return _submit_descriptor(content)
    if "change_set_id" in content:
        return _change_set(content)
    return [f"字段 {name}：{_size(value)}" for name, value in sorted(content.items())][:8]


def _decision_log(content: dict[str, Any]) -> list[str]:
    lines = [
        f"结论 {_text(content.get('decision_result'))}，完成度 {_text(content.get('status'))}",
        f"决策 {_count(content.get('decisions'))} 条，"
        f"验收点 {_count(content.get('acceptance_delta'))} 条，"
        f"未决前沿 {_count(content.get('unresolved_frontier'))} 条",
    ]
    lines += _named(content.get("decisions"), ("decision_id", "id"), "question", "决策")
    lines += _named(content.get("acceptance_delta"), ("acceptance_id", "id"), "statement", "验收")
    return lines


def _spec(content: dict[str, Any]) -> list[str]:
    return [
        f"版本 {_text(content.get('version'))}",
        f"行为 {_count(content.get('behaviors'))} 条，"
        f"验收场景 {_count(content.get('acceptance_scenarios'))} 条，"
        f"非目标 {_count(content.get('non_goals'))} 条",
        f"测试接口 {_count(content.get('test_interface'))} 项，"
        f"环境要求 {_count(content.get('environment_requirements'))} 项",
    ]


_REPO_TOKEN = re.compile(r"([a-z0-9][a-z0-9._-]*/[a-z0-9._-]+/[a-z0-9._-]+)")


def _dag_node_repos(node: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Repositories a task touches, split into business and test sides.

    The task-dag has no dedicated repository field; each node names its repos in
    the leading token of every business/test change line (e.g. baidu/sysip/bgwagent).
    Branch and pinned revision are deliberately absent here — they are bound at
    WorkspaceGate and shown on the G4/G7 cards — so this only surfaces module names.
    """
    def repos(changes: Any) -> list[str]:
        seen: list[str] = []
        for line in changes if isinstance(changes, list) else []:
            if not isinstance(line, str):
                continue
            match = _REPO_TOKEN.match(line.strip())
            if match and match.group(1) not in seen:
                seen.append(match.group(1))
        return seen

    return repos(node.get("business_changes")), repos(node.get("test_changes"))


def _task_dag(content: dict[str, Any]) -> list[str]:
    lines = [
        f"任务 {_count(content.get('nodes'))} 个，依赖 {_count(content.get('edges'))} 条",
        f"验收点覆盖 {_count(content.get('acceptance_coverage'))} 项",
    ]
    nodes = content.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        business, test = _dag_node_repos(node)
        parts = []
        if business:
            parts.append(f"业务 {'、'.join(business)}")
        if test:
            parts.append(f"测试 {'、'.join(test)}")
        repo_text = "；".join(parts) if parts else "仓库未标注"
        lines.append(f"{_text(node.get('task_id'))} {repo_text}")
    lines.append("分支与 pinned 版本在 WORKSPACE 绑定后于 G4/G7 卡展示")
    return lines


def _task_plan(content: dict[str, Any]) -> list[str]:
    return [
        f"任务 {_text(content.get('task_id'))}",
        f"仓库 {_count(content.get('repositories'))} 个，"
        f"文件 {_count(content.get('files'))} 个，"
        f"测试 {_count(content.get('tests'))} 项",
        f"验收点 {_count(content.get('acceptance_point_ids'))} 项",
    ]


def _change_set(content: dict[str, Any]) -> list[str]:
    # The deviations are the part of a diff an approver cannot reconstruct from the
    # Task Plan they already approved, so they are named rather than only counted.
    lines = [
        f"任务 {_text(content.get('task_id'))}",
        f"测试用例 {_count(content.get('test_ids'))} 项，"
        f"追溯更新 {_count(content.get('traceability_delta'))} 项，"
        f"偏离计划 {_count(content.get('deviations'))} 处",
        f"全量 diff 哈希 {_short(content.get('full_diff_hash'))}",
    ]
    return lines + _named(content.get("deviations"), (), "reason", "偏离")


def _is_submit_descriptor(content: dict[str, Any]) -> bool:
    """Distinguish the G7 submission descriptor from the G5 change-set artifact."""
    return all(
        isinstance(content.get(key), str) and content.get(key)
        for key in ("module", "target_branch", "commit_revision")
    ) and isinstance(content.get("revision_set"), dict)


def _submit_descriptor(content: dict[str, Any]) -> list[str]:
    """Show the identity an approver is authorizing iCode to submit."""
    revision_set = content.get("revision_set") or {}
    rows = []
    for role in ("business", "test"):
        repository = revision_set.get(role)
        if not isinstance(repository, dict):
            continue
        rows.append(
            f"{role} 仓库 {_text(repository.get('module'))}，"
            f"分支 {_text(repository.get('branch'))}，"
            f"版本 {_short(repository.get('revision'))}"
        )
    if content.get("change_number") or content.get("existing_cr"):
        mode = "追加现有 CR"
    elif content.get("submission_mode") == "create_new_cr":
        mode = "新建 CR"
    else:
        mode = "提交前由 iCode 校验新建/追加"
    return [
        f"提交方式 {mode}",
        f"主仓库 {_text(content.get('module'))}，分支 {_text(content.get('target_branch'))}",
        f"提交版本 {_short(content.get('commit_revision'))}",
        f"变更集 {_text(content.get('change_set_id'))}，版本集 {_short(content.get('revision_set_id'))}",
        *rows,
    ]


def _named(
    value: Any, id_keys: tuple[str, ...], text_key: str, label: str, limit: int = 4
) -> list[str]:
    if not isinstance(value, list):
        return []
    lines = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        identifier = next(
            (str(item[key]) for key in id_keys if isinstance(item.get(key), str)), ""
        )
        text = item.get(text_key)
        text = str(text) if isinstance(text, str) else ""
        head = " ".join(part for part in (label, identifier) if part)
        lines.append(f"{head}：{_clip(text)}" if text else head)
    remaining = len(value) - len(lines)
    if remaining > 0:
        lines.append(f"其余 {remaining} 条见产物文档")
    return lines


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def _size(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return f"{len(value)} 项"
    return _clip(str(value))


def _text(value: Any) -> str:
    return _clip(str(value)) if isinstance(value, (str, int, float)) else "-"


def _short(value: Any) -> str:
    return f"{value[:12]}…" if isinstance(value, str) and len(value) > 12 else _text(value)


def _clip(value: str, limit: int = 60) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
