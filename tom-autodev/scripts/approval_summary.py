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
    "WORKFLOW_SPEC_MIGRATION": (
        "旧新 workflow_spec_hash、保留的阶段/审批/外部审计绑定",
        "运行事件、已有 artifact、approval_id/input_hash 和外部 intent/receipt",
    ),
    "CHANGE_REQUEST": (
        "回退目标、原因、将重新产出的阶段；若同时改 profile，核对改动字段",
        "发起时的阶段事件、回退目标与 profile 旧新 hash；已有记录不删除",
    ),
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
    "CHANGE_REQUEST": (
        "执行另行 apply 后回到目标阶段并重新产出其后各阶段；每一版仍需各自的门，"
        "不批准任何草案，运行离开发起时的位置即失效"
    ),
    "WORKFLOW_SPEC_MIGRATION": (
        "执行另行 apply 后移动 workflow policy pin；不重写阶段事件、"
        "不创建新的 G1、不改变既有 approval_id/input_hash，也不删除外部审计"
    ),
}


def gate_intent(action: Any) -> dict[str, Any]:
    from approval_ledger import gate_of
    name = gate_of(str(action))
    subject, effect = _GATES.get(name, ("", ""))
    if name == "PROFILE_REPIN":
        subject = "将运行重新钉到已审核的 project profile"
    if name == "WORKFLOW_SPEC_MIGRATION":
        subject = "将暂停中的运行迁移到当前 workflow policy"
    if name == "CHANGE_REQUEST":
        subject = "变更请求：回到较早阶段重新产出"
    focus, reads = _REVIEW.get(name, ("核对本次动作与绑定", "缺少已知审批合同，请先补齐"))
    revisable = name in workflow_spec.REVISABLE_GATES
    return {"subject": subject, "effect": _EFFECTS.get(name, effect),
            "checklist": [focus, reads],
            "decision": (
                ("Comate 选择已经完成；本卡只审批已生成的 Decision Log，不替用户答题。"
                 "本次授权仍只绑定 input_hash，内容变化须重新审批。" if name == "G1" else
                 "同意仅授权本次 input_hash；内容变化须重新审批，审批本身不推进阶段。")
                + "同意继续当前 run；需修改保留 run、按意见重做并重发；驳回停止 run。"
            )}


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
        runtime = getattr(orchestrator, "_runtime_profile", lambda _run_id: None)(run_id)
        if not isinstance(runtime, dict) or not runtime.get("ok"):
            return _missing()
        profile = runtime.get("profile")
        profile_hash = runtime.get("profile_hash")
        if not isinstance(profile, dict) or not isinstance(profile_hash, str) or not profile_hash:
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
        binding = {
            "schema_version": "1",
            "run_id": run_id,
            "source_event_id": action.get("source_event_id"),
            "task_id": task_id,
            # Use the hash pinned by _runtime_profile.  Intake's profile_hash is
            # the historical intake value and may be different after profile
            # pinning/normalization; using it here makes a valid G4 binding fail
            # material reconstruction with APPROVAL_MATERIAL_MISSING.
            "profile_hash": profile_hash,
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


def _revision_material(orchestrator: Any, action: dict[str, Any]) -> dict[str, Any]:
    """Why a phase is drafted again and the version it replaces, for the diff."""
    reentry = action.get("reentry")
    if not isinstance(reentry, dict):
        return {}
    material: dict[str, Any] = {"reentry": reentry}
    job_id = reentry.get("previous_job_id")
    job = orchestrator.state.producer_job(job_id) if isinstance(job_id, str) and job_id else None
    previous = job.get("draft") if isinstance(job, dict) else None
    if isinstance(previous, dict):
        material["previous_draft"] = previous
    return material


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
            # G4 is used twice in the durable flow: first for WORKSPACE
            # ownership, then for the PLAN draft.  Once the PLAN producer has
            # fulfilled its job, the read-only frontier is PLAN rather than
            # WORKSPACE, so the workspace-binding reconstruction cannot match
            # the new approval hash.  Keep the historical WORKSPACE path, but
            # fall through to the current producer-draft path below when the
            # run has already advanced.
            workspace = _workspace_context(orchestrator, run_id, input_hash)
            if workspace.get("material_status") == "PINNED":
                return workspace
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
        if gate_name == "WORKFLOW_SPEC_MIGRATION":
            # This gate's object is the policy pin move itself, derived read-only
            # from the run's events; there is no phase artifact to read. Without
            # this branch a redelivery of the card reports APPROVAL_MATERIAL_MISSING
            # and a paused, drifted run can never be migrated at all.
            from workflow_spec_migration import plan as migration_plan

            prepared = migration_plan(orchestrator, run_id)
            if not prepared.get("ok") or prepared.get("input_hash") != input_hash:
                return missing
            return {
                "summary": [
                    f"workflow_spec_hash {prepared['old_hash'][:12]}… → {prepared['new_hash'][:12]}…",
                    f"当前阶段 {prepared['current_state']}",
                    *(f"保留 {item}" for item in prepared["preserves"]),
                ],
                "references": [
                    {"label": "旧 workflow_spec_hash", "ref": prepared["old_hash"]},
                    {"label": "当前 workflow_spec_hash", "ref": prepared["new_hash"]},
                    {"label": "当前阶段", "ref": str(prepared["current_state"])},
                ],
                "content_hash": input_hash,
                "material_status": "PINNED",
                "_review_content": {
                    key: prepared[key]
                    for key in (
                        "run_id", "action", "old_hash", "new_hash",
                        "current_state", "preserves", "input_hash",
                    )
                },
            }
        if gate_name == "CHANGE_REQUEST":
            from run_change import snapshot, summary_lines

            prepared = snapshot(orchestrator.state, run_id, input_hash)
            if prepared is None or prepared.get("input_hash") != input_hash:
                return missing
            return {
                "summary": summary_lines(prepared),
                "references": [
                    {"label": "当前阶段事件", "ref": str(prepared["from_event_id"])},
                    {"label": "回退目标", "ref": str(prepared["target"])},
                ],
                "content_hash": input_hash,
                "material_status": "PINNED",
                "_review_content": {"change_request": prepared},
            }
        action = _read_only_next(orchestrator, run_id)
        if (
            not action.get("ok")
            and gate_name == "G6"
            and action.get("reason_code") == "ACTION_CONFLICT"
            and action.get("state") == "DIAGNOSE"
        ):
            # A paused legacy DIAGNOSE action may differ only in the repaired,
            # producer-block-derived source refs. Reuse the same bounded verifier
            # as submission; never synthesize refs here.
            from worker_driver import _recover_legacy_diagnose_action
            candidates = [
                item for item in orchestrator.state.pending_producer_jobs(run_id)
                if (item.get("payload") or {}).get("phase") == "DIAGNOSE"
            ]
            repaired = orchestrator.phase_protocol().legacy_diagnose_action(run_id)
            if isinstance(repaired, dict) and repaired.get("ok"):
                historical_job = orchestrator.state.producer_job(
                    f"producer:{repaired.get('action_id')}"
                )
                if (
                    isinstance(historical_job, dict)
                    and historical_job.get("run_id") == run_id
                    and (historical_job.get("payload") or {}).get("phase") == "DIAGNOSE"
                    and historical_job.get("job_id") not in {item.get("job_id") for item in candidates}
                ):
                    candidates.append(historical_job)
            recovered = [
                _recover_legacy_diagnose_action(orchestrator, run_id, item.get("job_id"))
                for item in candidates
            ]
            recovered = [item for item in recovered if item is not None]
            if len(recovered) == 1:
                action = recovered[0]
        if (
            gate_name != "G9"
            and action.get("ok")
            and action.get("required_human_gate") == gate_name
            and (gate_name != "G4" or action.get("phase") == "PLAN")
        ):
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
                            "_review_content": {"draft": draft, **_revision_material(orchestrator, action)}}
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
    if _is_diagnosis(content):
        return _diagnosis_summary(content)
    if "change_set_id" in content:
        return _change_set(content)
    return [f"字段 {name}：{_size(value)}" for name, value in sorted(content.items())][:8]


def _decision_log(content: dict[str, Any]) -> list[str]:
    lines = [
        f"结论 {_text(content.get('decision_result'))}，完成度 {_text(content.get('status'))}",
        f"决策 {_count(content.get('decisions'))} 条，"
        f"验收点 {_count(content.get('acceptance_delta'))} 条，"
        f"未决前沿 {_count(content.get('unresolved_frontier'))} 条，"
        f"待选择题 {_count(content.get('choice_questions'))} 道",
    ]
    lines += _named(content.get("decisions"), ("decision_id", "id"), "question", "决策")
    for decision in content.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        options = "；".join(_clip(str(option)) for option in decision.get("options") or [])
        choice = _clip(str(decision.get("choice", "")))
        if options:
            lines.append(f"选项 {decision.get('id', '')}：{options}")
        if choice:
            lines.append(f"已选择 {decision.get('id', '')}：{choice}")
    for question in content.get("choice_questions") or []:
        if not isinstance(question, dict):
            continue
        option_ids = [
            option.get("id") for option in question.get("options") or []
            if isinstance(option, dict) and isinstance(option.get("id"), str)
        ]
        suffix = f"（选项：{'、'.join(option_ids)}）" if option_ids else ""
        lines.append(f"待选择 {question.get('id', '')}：{_clip(str(question.get('title', '')))}{suffix}")
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


def _is_diagnosis(content: dict[str, Any]) -> bool:
    return "failure_signature" in content and (
        "repair_plan" in content or "repair_direction" in content
    )


_DIAGNOSIS_ENUMS = {
    "PLAN_INCOMPLETE": "计划不完整",
    "SUFFICIENT": "证据充分",
    "INSUFFICIENT": "证据不足",
    "CODE_ONLY": "仅代码修复",
    "SPEC_AMENDMENT": "规格修订",
    "REPAIR": "修复",
    "SPEC": "规格修订路线",
    "PLAN": "计划修订路线",
    "ARCHITECTURE_REVIEW": "架构复核路线",
    "STOPPED": "停止流程",
}

_DIAGNOSIS_PHRASES = {
    "Expected: the implementation plan must encode the currently approved cookie validation behavior before code generation.":
        "预期：生成代码前，实现计划必须编码当前已批准的 Cookie 校验行为。",
    "Observed: the pinned PLAN still requires exact global/VS c2s_mss index equality, which contradicts the current conservative solution.":
        "观察：当前固定的实现计划仍要求全局与 VS 的 c2s_mss 索引严格相等，与当前保守方案冲突。",
    "No iPipe build, stage, job, environment, or runtime failure evidence exists; this is a planning-input mismatch.":
        "没有 iPipe 构建、阶段、作业、环境或运行时失败证据；本次属于计划输入不一致。",
    "Regenerate the task PLAN from the approved behavior: preserve cookie layout, PAL encoder, MSS tables, signing priority, reserved-bit and wscale validation, MSS-index bounds/non-zero checks, and remove global/VS exact c2s_mss equality.":
        "按已批准行为重新生成任务计划：保留 Cookie 布局、PAL 编码器、MSS 表、签名优先级、保留位与 wscale 校验、MSS 索引范围和非零校验，并移除全局与 VS 的 c2s_mss 严格相等要求。",
    "Verify the new PLAN explicitly maps each accepted behavior to business and independent product-test work without introducing a new cookie layout or local execution evidence.":
        "核对新计划是否将每项已接受行为明确映射到业务改动和独立产品测试，不引入新的 Cookie 布局，也不使用本地执行证据。",
    "The implementation is blocked because the pinned PLAN was approved before the final cookie-validation decision and still contains the removed exact c2s_mss equality requirement. The correct repair is to revise the specification/plan contract before generating code.":
        "当前实现被阻断，因为固定计划早于最终 Cookie 校验决策获批，仍包含已移除的 c2s_mss 严格相等要求。正确修复是在生成代码前修订规格与计划合同。",
    "Run the IMPLEMENT producer against the pinned PLAN and source revisions.":
        "基于固定计划和源码修订运行 IMPLEMENT 生产者。",
    "The producer precheck detects the approved PLAN and current user decision are behaviorally inconsistent.":
        "生产者前置检查发现已批准计划与当前用户决策在行为上不一致。",
    "No code patch is generated; the controller records a producer-block report and routes to DIAGNOSE.":
        "未生成代码补丁；控制器记录生产者阻断报告并路由到 DIAGNOSE。",
    "IMPLEMENT ProducerJob producer:dace0d7ed7627e624f40d3fc73e496d153667663ae2e662e38c217d45d89475d produced no DraftContent and no business or test changes.":
        "IMPLEMENT 生产任务未生成 DraftContent，也未产生业务或测试改动。",
    "The approved PLAN requires exact c2s_mss configuration index equality, while the current approved user decision removes global/VS exact-index equality and retains only cookie-field and MSS-index legality checks.":
        "已批准计划要求 c2s_mss 配置索引严格相等，而当前已批准用户决策移除了全局与 VS 的严格索引相等，仅保留 Cookie 字段和 MSS 索引合法性校验。",
    "The producer-block report bb28c94fe5274208fcb0e6980de49422507040b051ad9f737b54f19e3c388b35 records PLAN_INCOMPLETE with workspace_state NO_CODE_CHANGES.":
        "生产者阻断报告记录计划不完整，工作区状态为未产生代码改动。",
    "Require the normal G4 approval for the new plan before returning to IMPLEMENT.":
        "返回 IMPLEMENT 前，必须按正常流程取得新计划的 G4 审批。",
    "Do not patch code in DIAGNOSE. Re-enter PLAN and replace the stale exact c2s_mss equality requirement with the approved conservative validation behavior.":
        "不要在 DIAGNOSE 阶段直接修改代码。应重新进入 PLAN，用已批准的保守校验行为替换过时的 c2s_mss 严格相等要求。",
    "Update the task plan and acceptance traceability to match the final conservative cookie-validation decision.":
        "更新任务计划和验收追溯，使其符合最终的保守 Cookie 校验决策。",
    "Keep the change bounded to dataplane cookie validation and the independent product-test cases already identified by the task.":
        "将改动限制在数据面 Cookie 校验以及任务已识别的独立产品测试用例内。",
    "After G4 approval, regenerate the IMPLEMENT change set and validate it through source review and iPipe evidence.":
        "取得 G4 审批后，重新生成 IMPLEMENT 改动集，并通过源码评审和 iPipe 证据核验。",
}

_DISPLAY_ENUMS = {
    "COMPLETE": "已完成",
    "INCOMPLETE": "未完成",
    "PENDING": "待处理",
    "FULFILLED": "已生成",
    "SUFFICIENT": "证据充分",
    "INSUFFICIENT": "证据不足",
    "CONTRACT_INPUT_MISSING": "缺少合同输入",
    "NO_CODE_CHANGES": "未产生代码改动",
    "CODE_ONLY": "仅代码修复",
    "REPAIR": "修复",
    "REPAIR_SCOPE": "修复范围",
    "REVIEW": "评审",
    "PLAN": "计划",
    "SPEC": "规格说明",
    "DIAGNOSE": "诊断",
    "IMPLEMENT": "实现",
}



def _diagnosis_text(value: Any) -> str:
    if not isinstance(value, str):
        return _text(value)
    if value in _DIAGNOSIS_ENUMS:
        return _DIAGNOSIS_ENUMS[value]
    if value in _DIAGNOSIS_PHRASES:
        return _DIAGNOSIS_PHRASES[value]
    if value in _DISPLAY_ENUMS:
        return _DISPLAY_ENUMS[value]
    if value == "PLAN_INCOMPLETE: approved task plan requires removed global/VS c2s_mss exact-index equality":
        return "计划不完整：已批准计划仍包含已移除的全局与 VS c2s_mss 严格索引相等要求"
    if re.fullmatch(r"[A-Za-z0-9._:/@#-]+", value):
        return value
    if any("\u4e00" <= char <= "\u9fff" for char in value):
        return value
    # Word-level substitution dropped negations ("Do not remove" read as "移除"),
    # so an approver could approve the opposite of the hash-bound draft. Without an
    # exact translation, show the source verbatim and label it as untranslated.
    return f"（未翻译原文）{value}"


def _diagnosis_summary(content: dict[str, Any]) -> list[str]:
    lines = [
        f"诊断分类 {_diagnosis_text(content.get('classification'))}",
        f"证据状态 {_diagnosis_text(content.get('evidence_state'))}",
        f"修复范围 {_diagnosis_text(content.get('repair_scope'))}",
        f"后续路线 {_diagnosis_text(content.get('route'))}",
        f"失败签名 {_diagnosis_text(content.get('failure_signature'))}",
    ]
    if content.get("hypothesis") not in (None, ""):
        lines.append(f"根因假设 {_diagnosis_text(content['hypothesis'])}")
    for key, label in (("comparison", "对比证据"), ("repair_plan", "修复计划")):
        if isinstance(content.get(key), list):
            lines.append(f"{label} {len(content[key])} 条")
    return lines


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
    if not isinstance(value, (str, int, float)):
        return "-"
    return _clip(_diagnosis_text(str(value)))


def _short(value: Any) -> str:
    return f"{value[:12]}…" if isinstance(value, str) and len(value) > 12 else _text(value)


def _clip(value: str, limit: int = 60) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
