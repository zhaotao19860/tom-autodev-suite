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
    "G6": ("分类与路线是否成立，根因、修复方向与修复计划是否对应",
           "诊断的 classification / route / hypothesis / repair_direction / repair_plan；其余证据见材料包"),
    "G7": ("区分 iCode 提交与 iPipe 首次触发；精确版本、目标分支、模块和参数",
           "已归档 submit descriptor + Review，或当前模块 trigger binding；两者 hash 不通用"),
    "G8": ("失败重跑或人工阶段继续；build/stage、真实失败原因及脱敏参数；"
           "重新构建时看被替换的 build、产物探测结论、次数与原因",
           "当前阶段远程证据、case_failures、rerun binding 与参数来源；"
           "重新构建为 rebuild binding 与下载探测状态"),
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
    "G6": "按诊断路由回 GRILL / SPEC / PLAN / ARCHITECTURE_REVIEW 或 STOPPED；后续各门仍须分别通过",
    "G7": "仅执行本次 hash 绑定的 iCode 提交或单模块 iPipe 触发；不是两种操作的通用授权",
    "G8": "仅重跑或继续本次 build/stage 和参数绑定的阶段，或对同一 CR 版本重新构建一次；不批准新代码或发布",
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
                ("Agent 结构化选择已经完成；本卡只审批已生成的 Decision Log，不替用户答题。"
                 "本次授权仍只绑定 input_hash，内容变化须重新审批。" if name == "G1" else
                 "同意仅授权本次 input_hash；内容变化须重新审批，审批本身不推进阶段。")
                + "同意继续当前 run；需修改保留 run、按意见重做并重发；停止run 停止本 run。"
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
            # Only this task's worktrees: a run with several tasks owns one set per task,
            # and an earlier task's rows (still ACTIVE) would otherwise be read first.
            rows = connection.execute(
                "SELECT * FROM worktree_ownership WHERE run_id = ? AND task_id = ? AND status = 'ACTIVE'",
                (run_id, task_id),
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
        if gate_name == "G8":
            material = orchestrator.state.idempotency_result(
                f"ipipe.g8-material:{run_id}:{input_hash}"
            )
            if not isinstance(material, dict) or _hash(material.get("binding")) != input_hash:
                return missing
            binding = material["binding"]
            if binding.get("operation") == "ipipe.rebuild":
                probe = material.get("probe") or {}
                unverified = (material.get("revisions") or {}).get("unverified") or []
                return {
                    "summary": [
                        "操作 为同一 CR 版本重新触发整条流水线；仅授权这一次构建，不批准新代码或发布。",
                        f"模块 {_text(binding.get('module'))}，流水线 {_text(binding.get('pipeline_id'))}，"
                        f"第 {_text(binding.get('attempt'))} 次构建，触发类型 {_text(binding.get('event_type'))}",
                        f"被替换的构建 {_text(binding.get('original_build_id'))}；编译产物下载探测 "
                        f"{_text(probe.get('verdict'))} HTTP {_text(probe.get('http_status'))}",
                        f"原因 {_text(binding.get('reason'))}",
                        *(f"仓库 {_text(item.get('module'))}，分支 {_text(item.get('branch'))}，"
                          f"版本 {_text(item.get('revision'))}" for item in binding.get("repositories", [])),
                        *(f"未能核对当前 patchset：{_text(module)}" for module in unverified),
                    ],
                    "material_status": "PINNED", "content_hash": _hash(material),
                    "references": [], "repositories": binding.get("repositories", []),
                    "_review_content": material,
                }
            declaration = material.get("declaration") or {}
            return {
                "summary": [
                    "操作 继续人工 iPipe 阶段；仅授权本次 build/stage、版本和参数。",
                    f"模块 {_text(binding.get('module'))}，流水线 {_text(binding.get('pipeline_id'))}",
                    f"构建 {_text(binding.get('build_id'))}，阶段 {_text(binding.get('stage_build_id'))}"
                    f" {_text(declaration.get('stage_name'))}",
                    f"参数声明来源 {_text(declaration.get('source'))}",
                    *(["注意：流水线实时配置已删除此 stage，按本次 build 自带的显式空声明执行"]
                      if declaration.get("config_stage_missing") else []),
                    *(f"仓库 {_text(item.get('module'))}，分支 {_text(item.get('branch'))}，"
                      f"版本 {_text(item.get('revision'))}" for item in binding.get("repositories", [])),
                    *(f"参数 {name} = {value}" for name, value in material.get("parameters", {}).items()),
                    *([] if material.get("parameters") else ["平台明确声明空参数，执行输入 {}"]),
                ],
                "material_status": "PINNED", "content_hash": _hash(material),
                "references": [], "repositories": binding.get("repositories", []),
                "_review_content": material,
            }
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
        if gate_name == "G7":
            trigger = _trigger_context(orchestrator, run_id, input_hash)
            if trigger is not None:
                return trigger
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        return missing
    return missing


def _trigger_context(orchestrator: Any, run_id: str, input_hash: str) -> dict[str, Any] | None:
    """The G7 iPipe trigger a hash names, rebuilt from the frozen plan without transport.

    Without it a trigger approval had nothing to show and could never be delivered.
    """
    from clients.ipipe_runtime import _context, trigger_binding
    from pipeline_plan import frozen_plan

    plan = frozen_plan(orchestrator.state.events(run_id))
    pinned = orchestrator._runtime_profile(run_id)
    if plan is None or not pinned.get("ok"):
        return None
    for module in plan["required_modules"]:
        context = _context(pinned["profile"], plan["revision_set"], run_id, module)
        if context.get("reason_code") != "OK":
            continue
        binding = trigger_binding(context, run_id)
        if _hash(binding) != input_hash:
            continue
        rows = [
            f"{item['kind']} 仓库 {_text(item['module'])}，分支 {_text(item['branch'])}，"
            f"版本 {_short(item['revision'])}"
            for item in binding["repositories"]
        ]
        unchanged = sorted((plan.get("unchanged") or {}).keys())
        return {
            "summary": [
                "操作 触发 iPipe 流水线（平台上不存在该版本的构建，需新建一次）",
                f"模块 {_text(binding['module'])}，流水线 {_text(binding['pipeline_id'])}",
                f"版本集 {_short(binding['revision_set_id'])}",
                *rows,
                *(f"未变更仓库 {_text(name)} 不触发流水线" for name in unchanged),
            ],
            "references": [{"label": "冻结流水线计划", "ref": str(plan["plan_hash"])}],
            "content_hash": input_hash,
            "material_status": "PINNED",
            "_review_content": {"trigger_binding": binding},
        }
    return None


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
    "GRILL": "回到需求研判（GRILL）修订决策日志",
    "USER_REQUIREMENT_CHANGED": "需求变更",
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

# Exact translations for the diagnosis currently delivered by BGW-1995 G6.
# Keep this table presentation-only: the canonical DraftContent and its hashes
# must continue to contain the producer's original evidence.
_BGW_1995_DIAGNOSIS_TRANSLATIONS = {
    "REVIEW:R1:undeclared svc in synproxy_should_reuse_session call path;R2:approved T1 malformed-cookie/lower MSS-index negative test deliverable absent":
        "评审问题：R1，synproxy_should_reuse_session 调用路径中的 svc 未声明；R2，已批准的 T1 畸形 Cookie 与较低 MSS 索引负向测试交付物缺失。",
    "The current Review rejection has two independently evidenced causes. R1: adapting the shared cookie validator to require service context without propagating bgw_svc_t *svc through synproxy_should_reuse_session leaves the reuse-path expression outside the identifier's scope, causing a deterministic compilation defect. A declaration supplying the intended service in that exact pinned scope would refute this explanation; the Review's scope/caller searches found none. R2: the approved negative acceptance matrix was not converted into a runnable independent QA fixture and assertions; ordinary curl positives, comments and arbitrary ACK rejection cannot isolate the new constraints. Finding those actual assertions and a valid fresh-cookie fixture in the frozen QA revision would refute this explanation; the complete Review found no such implementation. Evidence is sufficient for a complete source diagnosis and bounded repair proposal, not for a passing compile/test or release claim.":
        "当前评审被拒有两个分别得到证据支持的原因。R1：共享 Cookie 校验器新增服务上下文要求后，没有将 bgw_svc_t *svc 贯穿传入 synproxy_should_reuse_session，导致复用路径中的表达式超出该标识符的作用域，形成确定性的编译缺陷。若在当前固定作用域找到提供目标服务的声明，即可推翻这一解释；评审对作用域和调用方的搜索没有找到该声明。R2：已批准的负向验收矩阵没有转换为可运行的独立 QA fixture 和断言；普通 curl 正向流量、注释以及任意 ACK 拒绝无法隔离新增约束。若在固定 QA 修订中找到这些实际断言和有效的新鲜 Cookie fixture，即可推翻这一解释；完整评审没有找到相关实现。现有证据足以完成源码诊断并提出有界修复方案，但不足以声称编译、测试通过或可以发布。",
    "Keep the approved T1 behavior and use CODE_ONLY repair through parent-controlled PLAN/IMPLEMENT. In the business file, add an explicit borrowed bgw_svc_t *svc parameter to the private reuse helper, pass the existing service pointer from synproxy_session_process_in, and forward it to syn_proxy_cookie_check. In the independent QA repository, implement the missing deterministic fresh-cookie fixture and T1 negative assertions while retaining existing positives. Do not change cookie layout, signer, MSS tables, strict-index policy, public ABI, ownership or configuration synchronization to work around these findings. Diagnosis is complete with sufficient evidence; repair execution and verification remain pending.":
        "保留已批准的 T1 行为，并由父流程控制 PLAN/IMPLEMENT，按 CODE_ONLY 范围修复。业务文件中，为私有复用 helper 增加显式借用的 bgw_svc_t *svc 参数，从 synproxy_session_process_in 传入现有服务指针，再转发给 syn_proxy_cookie_check。独立 QA 仓库中，实现缺失的确定性新鲜 Cookie fixture 和 T1 负向断言，同时保留已有正向用例。不得为绕过这些问题而修改 Cookie 布局、签名器、MSS 表、严格索引策略、公共 ABI、所有权或配置同步。诊断已有充分证据并已完成；修复执行和验证仍待进行。",
    "OBSERVED — Current context binds DIAGNOSE action 240ddeed018ccbe6ac998bbf6d5cd74e297fb0f57c1197feeca00fe61a7ad8e0, input hash f569b12435e9b3a3b8809abaa2ae8a23c79b17ce166cad6e4fa64b1fc437b2eb, and source event 43f1ae5bf6aa471ea7fe8b95590b7573 to task BGW-1995-T1 in run 81a91c0842c74cbc910fcce375982e33. The producer job is PENDING with no draft present.":
        "实际观察：当前上下文将 DIAGNOSE action `240ddeed018ccbe6ac998bbf6d5cd74e297fb0f57c1197feeca00fe61a7ad8e0`、input hash `f569b12435e9b3a3b8809abaa2ae8a23c79b17ce166cad6e4fa64b1fc437b2eb` 和 source event `43f1ae5bf6aa471ea7fe8b95590b7573` 绑定到 run `81a91c0842c74cbc910fcce375982e33` 中的任务 BGW-1995-T1。生产任务状态为待处理，当前没有草稿。",
    "OBSERVED — The artifact reader reports pinned Review d44c15537f7cca7889bb39613aa8f8a886af8e7a7152f92b17d607ce4f14d581 as valid, with content hash 326878914344a2438fd0a845aba9e64f17ec2134344576ee6c58e192cc9a396d and the frozen revisions above. Its verdict is REJECT and completeness_state is COMPLETE. Only R1 and R2 are confirmed blocking findings. Supporting local records are /Users/tom/.tom-autodev/BGW-1995-T1-review-20261007.json and /Users/tom/.tom-autodev/BGW-1995-T1-review-20261007-evidence.md.":
        "实际观察：artifact reader 报告固定 Review `d44c15537f7cca7889bb39613aa8f8a886af8e7a7152f92b17d607ce4f14d581` 有效，content hash 为 `326878914344a2438fd0a845aba9e64f17ec2134344576ee6c58e192cc9a396d`，固定修订如上。评审结论为拒绝，完整性状态为已完成。只有 R1 和 R2 被确认是阻断问题。支持性本地记录为 `/Users/tom/.tom-autodev/BGW-1995-T1-review-20261007.json` 和 `/Users/tom/.tom-autodev/BGW-1995-T1-review-20261007-evidence.md`。",
    "OBSERVED — R1, P1: /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_synproxy.c:1977-1995,2126-2144. The changed expression is syn_proxy_cookie_check(ctx, skb, th, cookie, opt, svc), but synproxy_should_reuse_session accepts only ctx, cp, skb and opt. Its caller remains synproxy_should_reuse_session(ctx, cp, skb, &opt). The pinned Review records no local, file-scope or macro declaration supplying svc; synproxy_session_process_in already has the required service pointer. The initial-ACK call at lines 1618-1619 was adapted.":
        "实际观察：R1、P1：`/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_synproxy.c:1977-1995,2126-2144`。改动后的表达式是 `syn_proxy_cookie_check(ctx, skb, th, cookie, opt, svc)`，但 synproxy_should_reuse_session 只接收 ctx、cp、skb 和 opt。调用方仍为 `synproxy_should_reuse_session(ctx, cp, skb, &opt)`。固定评审没有记录提供 svc 的局部、文件作用域或宏声明；synproxy_session_process_in 已经拥有所需的服务指针。第 1618-1619 行的初始 ACK 调用已完成适配。",
    "OBSERVED — R2, P2: /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/case_dataflow_serial/test_dataflow_NAT44_tcp_040.py:45-126 exercises ordinary curl traffic with global C2S_MSS 536/1452/0. Lines 117-123 document the missing rejection fixture rather than implementing it. Reviewed delegates do not construct the required fresh-cookie rejection pairs or a controlled below-selected-index client case.":
        "实际观察：R2、P2：`/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/case_dataflow_serial/test_dataflow_NAT44_tcp_040.py:45-126` 只执行全局 C2S_MSS 为 536/1452/0 的普通 curl 流量。第 117-123 行记录了缺失的拒绝 fixture，但没有实现它。已评审的委托内容没有构造所需的新鲜 Cookie 拒绝对以及受控的低于选定索引的客户端用例。",
    "OBSERVED — Approved Plan cfa9536952dd0abfb806ff4542f5b6d7bf05b86d4890626f7b23d65d7e9c2e7c, content hash ec4e28df006dbe2714c6144e131e00ea5bc33bacb69055f537401aa5547971cd, explicitly requires malformed-cookie rejection, lower configured MSS-index rejection, non-RTT reserved-bit versus RTT-data distinction, and equivalent new-session/reuse results. It describes fixture inputs but supplies no concrete fixture identifier. Review evidence sections 4-5 establish the delivered QA fixture/assertion gap.":
        "实际观察：已批准计划 `cfa9536952dd0abfb806ff4542f5b6d7bf05b86d4890626f7b23d65d7e9c2e7c`、content hash `ec4e28df006dbe2714c6144e131e00ea5bc33bacb69055f537401aa5547971cd` 明确要求拒绝畸形 Cookie、拒绝低于配置的 MSS 索引、区分非 RTT 保留位与 RTT 数据含义，并要求新会话与复用得到等价结果。计划描述了 fixture 输入，但没有提供具体 fixture 标识。评审证据第 4-5 节确认已交付 QA fixture/断言存在缺口。",
    "OBSERVED — /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/packet_abnormal.py:650-663 uses arbitrary ack=222 without a fresh signed SYN/ACK. Rejection can therefore precede the newly added constraints and cannot serve as their discriminating oracle.":
        "实际观察：`/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/packet_abnormal.py:650-663` 使用任意 ack=222，没有新鲜的已签名 SYN/ACK。因此拒绝可能早于新增约束发生，不能作为区分这些约束的判定依据。",
    "HISTORY — One earlier persisted diagnosis exists in /Users/tom/.tom-autodev/artifacts/81a91c0842c74cbc910fcce375982e33/diagnose/ed4323423257bf51494685b97d88da99068728fac59d755e5cfe44b4ecfb45a2/content.bin. It records attempt 1, classification PLAN_INCOMPLETE, and the verbatim signature: PLAN_INCOMPLETE: approved task plan requires removed global/VS c2s_mss exact-index equality. It concerns an earlier planning-input conflict at different source revisions, not the current R1/R2 source Review. This draft uses attempt 2 without erasing that history, declaring the earlier case resolved, or resetting controller budgets.":
        "历史记录：`/Users/tom/.tom-autodev/artifacts/81a91c0842c74cbc910fcce375982e33/diagnose/ed4323423257bf51494685b97d88da99068728fac59d755e5cfe44b4ecfb45a2/content.bin` 中存在一份较早的持久化诊断。它记录第 1 次尝试、分类 PLAN_INCOMPLETE，以及原样签名：计划不完整：已批准任务计划要求已移除的全局与 VS c2s_mss 严格索引相等。该记录涉及不同源码修订下较早的计划输入冲突，不是当前 R1/R2 源码评审。本草稿使用第 2 次尝试，没有删除历史、宣称早期问题已解决或重置控制器预算。",
    "OBSERVED — This is a source-only Review occurrence recorded on 2026-10-07, not an observed compiler or product-test execution. No build/stage/job identity, runner fingerprint, first runtime failure timestamp or runtime log window exists in the current failure bundle; the corresponding execution fields are null.":
        "实际观察：这是 2026-10-07 记录的仅源码评审事件，不是已观察到的编译器或产品测试执行。当前失败材料中没有 build/stage/job 标识、runner 指纹、首次运行时失败时间或运行时日志窗口；对应执行字段均为 null。",
    "SOURCE REPRODUCTION — At the frozen business revision, trace synproxy_session_process_in → synproxy_should_reuse_session → syn_proxy_cookie_check. The first divergent boundary is the reuse caller/helper interface: the service context required by the newly changed validator call is not passed into the helper's scope.":
        "源码复现：在固定业务修订上跟踪 synproxy_session_process_in → synproxy_should_reuse_session → syn_proxy_cookie_check。第一个分歧边界是复用调用方/helper 接口：新增校验器调用所需的服务上下文没有传入 helper 作用域。",
    "SOURCE REPRODUCTION — Compare approved Plan checklist 5 and S1/S2 with the frozen QA entry and its reviewed delegates. The first divergent delivery boundary is approved negative-test requirement → runnable fixture/assertions: positive dataflow observations and comments were delivered, but the targeted rejection matrix was not.":
        "源码复现：将已批准计划检查项 5 及 S1/S2 与固定 QA 条目和已评审委托内容对照。第一个分歧交付边界是已批准负向测试要求 → 可运行 fixture/断言：已交付正向数据流观察和注释，但没有交付目标拒绝矩阵。",
    "NOT EXECUTED — No project compilation, tests, packet injection, remote commands, iCode/iPipe operations, draft submission or G6 approval were performed. No files were created or modified; this response is DraftContent only.":
        "未执行：没有进行项目编译、测试、报文注入、远程命令、iCode/iPipe 操作、草稿提交或 G6 审批。没有创建或修改文件；本响应仅为 DraftContent。",
    "EXPECTED versus OBSERVED — Both initial-ACK and reuse paths must pass the actual service context to the shared validator. The initial path does; the reuse helper references an undeclared svc. INFERENCE: compiling this translation unit as represented by the pinned source fails identifier resolution. This is a deterministic source defect, not a quoted compiler diagnostic or proof of runner reproduction.":
        "预期与实际对比：初始 ACK 和复用路径都必须将实际服务上下文传给共享校验器。初始路径已经传入；复用 helper 引用了未声明的 svc。推断：按固定源码编译该翻译单元会因标识符解析失败。这是确定性的源码缺陷，不是引用的编译器诊断，也不是 runner 已复现的证明。",
    "EXPECTED versus OBSERVED — T1 must deliver runnable external assertions for malformed cookies and below-selected MSS indices. The frozen QA change does not. INFERENCE: running that unchanged QA revision in iPipe cannot establish assertions absent from its source. This is a companion TEST_FAILURE in test delivery, not a failure caused by refusing local execution.":
        "预期与实际对比：T1 必须为畸形 Cookie 和低于选定值的 MSS 索引交付可运行的外部断言。固定 QA 改动没有做到。推断：在 iPipe 中运行未改变的 QA 修订，无法建立其源码中不存在的断言。这是测试交付上的伴随 TEST_FAILURE，不是拒绝本地执行导致的失败。",
    "BASELINE — Review compares business 4ff735f42187a596619858df2d4dfa64c4a69801 and tests 879cec4c3b46a2efaff199ca7a9d2f55e4e85bc8 with the frozen candidates. Those are source baselines, not demonstrated passing executions. No comparable passing occurrence with identical fixture, toolchain, configuration and topology was supplied.":
        "基线：评审将业务 `4ff735f42187a596619858df2d4dfa64c4a69801` 和测试 `879cec4c3b46a2efaff199ca7a9d2f55e4e85bc8` 与固定候选进行对比。它们是源码基线，不是已证明通过的执行。没有提供 fixture、工具链、配置和拓扑完全相同的可比通过记录。",
    "COUNTER-EVIDENCE — R3-R5 refute the suspected MSS-table off-by-one, persistent DSR/configuration mismatch, and QA type/unpacking/capture-contract defects. Last valid MSS indices remain 15 and 3; non-RTT zero-valued entries 0/1 differ from valid RTT index 0. Existing NAT44/NAT66 MSS, WSCALE/DSR and RTT-positive coverage must be retained rather than described as absent.":
        "反证：R3-R5 排除了疑似 MSS 表下标偏移、持久化 DSR/配置不一致以及 QA 类型/解包/抓包合同缺陷。最后有效 MSS 索引仍为 15 和 3；非 RTT 的零值条目 0/1 与有效 RTT 索引 0 不同。已有 NAT44/NAT66 MSS、WSCALE/DSR 和 RTT 正向覆盖必须保留，不能描述为缺失。",
    "SCOPE — R6 excludes S3 offsets, MSS=1344/offset=5376 and HTTP-400 regression from T1. The earlier PLAN_INCOMPLETE diagnosis is retained as history but does not override the currently pinned approved Plan and confirmed Review. Removing strict configured-index rejection would require an explicit parent-owned behavioral decision, not this CODE_ONLY repair.":
        "范围：R6 将 S3 偏移、MSS=1344/offset=5376 以及 HTTP-400 回归排除在 T1 外。较早的 PLAN_INCOMPLETE 诊断作为历史保留，但不覆盖当前固定的已批准计划和已确认评审。移除严格配置索引拒绝需要父流程明确作出行为决策，不属于本次 CODE_ONLY 修复。",
    "UNKNOWN — Actual compiler diagnostics, runner/toolchain availability, runtime rejection results, concrete negative-fixture implementation and its hash remain unobserved. Configuration-transition behavior is an acknowledged risk, not a newly confirmed root cause. These limitations prevent claiming a verified fix but do not prevent complete diagnosis of the two established source/delivery defects.":
        "未知：实际编译器诊断、runner/工具链可用性、运行时拒绝结果、具体负向 fixture 实现及其哈希仍未观察到。配置切换行为是已知风险，不是新确认的根因。这些限制使我们不能声称修复已验证，但不妨碍完成对两个已确定源码/交付缺陷的诊断。",
    "Before execution, perform source Review of the repaired candidate: verify all reuse-helper declarations/callers forward the same valid service pointer, preserve ownership/lifetime and public ABI, and map every approved T1 rejection clause to an executable QA assertion with a pinned fixture identity. This inspection is not project build/test evidence.":
        "执行前，对修复候选进行源码评审：核对所有复用 helper 声明和调用方都转发同一个有效服务指针，保持所有权、生命周期和公共 ABI 不变，并将每条已批准的 T1 拒绝条款映射到带固定 fixture 标识的可执行 QA 断言。该检查不是项目构建/测试证据。",
    "R1 discriminating verification: require complete compile logs for the repaired translation unit, followed by external initial-ACK and session-reuse positive/negative cases. A compile-only pass does not establish correct service selection or reuse behavior.":
        "R1 区分性验证：要求修复后翻译单元的完整编译日志，然后执行外部初始 ACK 和会话复用正向/负向用例。仅编译通过不能证明服务选择或复用行为正确。",
    "R2 discriminating verification: use controlled fresh SYN/SYN-ACK/ACK exchanges and one changed condition per paired case. Establish a successful control first, then show malformed reserved bits, invalid wscale pairs/configuration, non-RTT zero MSS entries and below-selected MSS index are rejected for the intended constraint rather than stale time, bad sequence or unrelated hash failure. Include a naturally signed lower-client-MSS case. If the fixture cannot isolate the constraint, report the fixture limitation instead of counting any dropped ACK as success.":
        "R2 区分性验证：使用受控的新鲜 SYN/SYN-ACK/ACK 交换，每对用例只改变一个条件。先建立成功控制组，再证明畸形保留位、无效 wscale 对/配置、非 RTT 零 MSS 条目和低于选定值的 MSS 索引是因目标约束被拒绝，而不是因过期时间、错误序列号或无关哈希失败被拒绝。加入自然签名的低客户端 MSS 用例。若 fixture 无法隔离约束，应报告 fixture 限制，不要把任意丢弃的 ACK 计为成功。",
    "Retain correlated packets, configuration snapshots and externally observed handshake/session outcomes. Invalid initial ACKs must not create a successful session; invalid reuse attempts must not establish replacement dataflow or corrupt the existing session. Record case IDs, timestamps, topology, toolchain/image identity, exact server and QA revisions, stage/job IDs and complete logs. Treat environment or revision mismatch as collection/environment failure rather than product rejection.":
        "保留关联报文、配置快照以及外部观察到的握手/会话结果。无效初始 ACK 不得建立成功会话；无效复用尝试不得建立替代数据流或损坏现有会话。记录用例 ID、时间戳、拓扑、工具链/镜像标识、准确的服务端和 QA 修订、stage/job ID 及完整日志。将环境或修订不一致视为采集/环境失败，而不是产品拒绝。",
    "If the parent uses P0新case回归, bind get_bgw_test_case to the exact repaired QA product and get_bgwagent to the compatible dependency product, and verify both are actually consumed. Earlier stages must supply the matching server binary; the manual case stage updates client artifacts only. Stop on an invalid positive control or widespread valid-handshake rejection; do not use blind reruns or a local build as substitute evidence.":
        "如果父流程使用 P0新case回归，将 get_bgw_test_case 绑定到准确的修复后 QA 产品，将 get_bgwagent 绑定到兼容的依赖产品，并核对两者确实被使用。前置阶段必须提供匹配的服务端二进制；手工用例阶段只更新客户端产物。若正向控制无效或大量有效握手被拒绝，应停止；不得用盲目重跑或本地构建替代证据。",
    "After the parent obtains the required approvals, execute only in the approved iPipe bindings: business baidu/sysip/x86bgw, branch x86bgw-dpdk-2011-syncookie, pipeline 348102 for compile/scan/unit/regression; independent QA baidu/nsiqa/x86bgw, branch syncookie-bugfix, pipeline 504074 for the repaired test product; compatible bgwagent dependency from pipeline 348142 without agent source changes. Bind exact repaired revisions, products and fixture/test-plan hashes. No pipeline call is authorized by this draft.":
        "父流程取得所需审批后，只能在已批准的 iPipe 绑定中执行：业务为 baidu/sysip/x86bgw，分支为 x86bgw-dpdk-2011-syncookie，pipeline 348102 用于 compile/scan/unit/regression；独立 QA 为 baidu/nsiqa/x86bgw，分支为 syncookie-bugfix，pipeline 504074 用于修复后的测试产品；兼容的 bgwagent 依赖来自 pipeline 348142，不修改 agent 源码。绑定准确的修复后修订、产品以及 fixture/测试计划哈希。本草稿不授权任何 pipeline 调用。",
    "Bound the product matrix to approved T1 behavior: RTT/non-RTT, initial session/reuse, IPv4/IPv6, nonzero VS override/global fallback, selected-index equality versus lower index, legal highest indices, valid RTT index 0, c2s_mss=0 and existing WSCALE/DSR positive regressions. Confirm non-RTT bits 12-13 are reserved while RTT high bits retain their RTT-data meaning. Exclude S3 offset and HTTP-400 acceptance.":
        "将产品矩阵限定为已批准的 T1 行为：RTT/非 RTT、初始会话/复用、IPv4/IPv6、非零 VS 覆盖/全局回退、选定索引相等与较低索引、合法最高索引、有效 RTT 索引 0、c2s_mss=0 以及现有 WSCALE/DSR 正向回归。确认非 RTT 的第 12-13 位为保留位，而 RTT 高位保留 RTT 数据含义。排除 S3 偏移和 HTTP-400 验收。",
    "PLAN — Preserve AC-GRILL-1 and AC-GRILL-3 and explicitly account for R1/R2. Specify fixture construction, external oracles, fresh-cookie timing, controlled reuse state, configuration isolation, case IDs, packet/session evidence and cleanup ownership. If a required negative cannot be constructed reliably, surface that bounded fixture blocker rather than silently deleting the approved requirement.":
        "计划：保留 AC-GRILL-1 和 AC-GRILL-3，并明确处理 R1/R2。规定 fixture 构造、外部判定、fresh-cookie 时序、受控复用状态、配置隔离、用例 ID、报文/会话证据和清理责任。若某项必需负向用例无法可靠构造，应暴露该有界 fixture 阻塞，不得静默删除已批准要求。",
    "BUSINESS SCOPE — Modify /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_synproxy.c at synproxy_should_reuse_session and its caller. Preserve the existing service lifetime supplied through /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_tcp.c; no allocation, reference transfer or public-header change is expected.":
        "业务范围：修改 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_synproxy.c` 中的 synproxy_should_reuse_session 及其调用方。保留通过 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/0a140c2f8ada3e5b/dataplane/bgw_tcp.c` 提供的现有服务生命周期；不应需要分配、引用转移或公共头文件变更。",
    "QA SCOPE — Extend /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/case_dataflow_serial/test_dataflow_NAT44_tcp_040.py and, only where necessary, existing helpers in /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/check.py. Inspect and reuse /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/packet_abnormal.py and /Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/scapy_op.py only as justified by the fixture design; arbitrary ack=222 is not an adequate replacement. Any additional family/RTT case-file edits must be explicitly bounded in the approved repair plan.":
        "QA 范围：扩展 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/case_dataflow_serial/test_dataflow_NAT44_tcp_040.py`，仅在必要时修改 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/check.py` 中已有 helper。仅按 fixture 设计需要检查和复用 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/packet_abnormal.py` 和 `/Users/tom/.tom-autodev/worktrees/81a91c0842c74cbc910fcce375982e33/BGW-1995-T1/db6e4d76d569f9e6/bgw_auto/lib/scapy_op.py`；任意 ack=222 不是合适替代。任何额外 family/RTT 用例文件改动都必须在已批准修复计划中明确限定。",
    "HANDOFF — The parent owns G6, retry-budget decisions and routing. After approval, PLAN/IMPLEMENT produces coordinated business/QA patches, followed by fresh source Review and G5 binding of the actual candidate. Only subsequent authorized iPipe evidence can close the repair. No repair diff currently exists, so repair_diff_hash remains null.":
        "交接：父流程负责 G6、重试预算决策和路由。审批后，PLAN/IMPLEMENT 生成协调的业务/QA 补丁，随后进行新的源码评审并对实际候选绑定 G5。只有之后获得授权的 iPipe 证据才能关闭修复。当前不存在修复 diff，因此 repair_diff_hash 仍为 null。",
    "ROLLBACK — If the repair is unsuitable, revert only its coordinated business/QA patch to the frozen candidate revisions for investigation; those candidates remain blocked and must not be released. If T1 must be abandoned or broad handshake rejection occurs, use the approved controlled rollback to business 4ff735f42187a596619858df2d4dfa64c4a69801 and tests 879cec4c3b46a2efaff199ca7a9d2f55e4e85bc8, retaining failure evidence and restoring test configuration through fixture teardown. Do not rewrite pipeline templates, replace a server binary manually, alter PAL wire semantics or bypass gateway validation. Deployment rollback remains parent-authorized and iPipe-only.":
        "回滚：若修复不合适，仅将协调的业务/QA 补丁回退到固定候选修订以便调查；这些候选仍被阻断，不得发布。若必须放弃 T1 或出现大范围握手拒绝，使用已批准的受控回滚，业务回到 `4ff735f42187a596619858df2d4dfa64c4a69801`，测试回到 `879cec4c3b46a2efaff199ca7a9d2f55e4e85bc8`，保留失败证据，并通过 fixture teardown 恢复测试配置。不得重写 pipeline 模板、手工替换服务端二进制、改变 PAL 线协议语义或绕过网关校验。部署回滚仍由父流程授权且只能通过 iPipe 执行。",
}

_DIAGNOSIS_STRUCTURAL_LABELS = {
    "OBSERVED": "实际观察",
    "EXPECTED": "预期结果",
    "EXPECTED versus OBSERVED": "预期与实际对比",
    "SOURCE REPRODUCTION": "源码复现",
    "LOG EVIDENCE": "日志证据",
    "REPRODUCTION": "复现步骤",
    "MINIMAL VERIFICATION": "最小验证",
    "REPAIR PLAN": "修复计划",
}

_DIAGNOSIS_SAFE_PATTERNS = (
    (
        re.compile(
            r"^The pinned PLAN still requires exact global/VS c2s_mss index equality"
            r", which contradicts the current conservative solution\.$"
        ),
        "当前固定计划仍要求全局与 VS 的 c2s_mss 索引严格相等，"
        "与当前保守方案冲突。",
    ),
    (
        re.compile(
            r"^No iPipe build, stage, job, environment, or runtime failure evidence exists; "
            r"this is a planning-input mismatch\.$"
        ),
        "没有 iPipe 构建、阶段、作业、环境或运行时失败证据；"
        "本次属于计划输入不一致。",
    ),
    (
        re.compile(
            r"^Do not patch code in DIAGNOSE\. Re-enter PLAN and replace the stale "
            r"exact c2s_mss equality requirement with the approved conservative validation behavior\.$"
        ),
        "不要在 DIAGNOSE 阶段直接修改代码。应重新进入 PLAN，"
        "用已批准的保守校验行为替换过时的 c2s_mss 严格相等要求。",
    ),
    (
        re.compile(r"^Do not patch code in DIAGNOSE\. Re-enter PLAN\.$"),
        "不要在 DIAGNOSE 阶段直接修改代码，应重新进入 PLAN。",
    ),
)

_DIAGNOSIS_TECHNICAL_TRANSLATIONS = (
    # Mixed Chinese/technical diagnostic prose is safe to render once the
    # bounded English vocabulary is translated. Keep protocol names and code
    # identifiers unchanged for auditability.
    (re.compile(r"\bfresh signed\b", re.IGNORECASE), "新鲜签名的"),
    (re.compile(r"\bsigned\b", re.IGNORECASE), "已签名的"),
    (re.compile(r"\bfixture(s)?\b", re.IGNORECASE), "测试夹具"),
    (re.compile(r"\btuple\b", re.IGNORECASE), "连接四元组"),
    (re.compile(r"\bhash\b", re.IGNORECASE), "哈希"),
    (re.compile(r"\btime-window\b", re.IGNORECASE), "时间窗口"),
    (re.compile(r"\bsequence-low\b", re.IGNORECASE), "序列号低位"),
    (re.compile(r"\barbitrary\b", re.IGNORECASE), "任意构造的"),
    (re.compile(r"\bcontrol\b", re.IGNORECASE), "对照"),
    (re.compile(r"\bnegative\b", re.IGNORECASE), "负向"),
    (re.compile(r"\bpositive\b", re.IGNORECASE), "正向"),
    (re.compile(r"\bvalidator\b", re.IGNORECASE), "校验器"),
    (re.compile(r"\bhelper surface\b", re.IGNORECASE), "辅助接口"),
    (re.compile(r"\bhelper\b", re.IGNORECASE), "辅助函数"),
    (re.compile(r"\bpacket/session\b", re.IGNORECASE), "报文/会话"),
    (re.compile(r"\bACK\b", re.IGNORECASE), "ACK"),
    (re.compile(r"\bSYN-ACK\b", re.IGNORECASE), "SYN-ACK"),
    (re.compile(r"\bSYN\b", re.IGNORECASE), "SYN"),
)

_MIXED_DIAGNOSIS_TERMS = {
    "fresh signed": "新鲜签名的", "Change Set": "变更集",
    "Plan artifact": "任务计划产物", "Plan content hash": "任务计划内容哈希",
    "action": "动作",
    "source event": "来源事件", "source": "来源",
    "source Review": "源码评审",
    "source event": "来源事件", "source revisions": "来源修订",
    "IMPLEMENT ProducerJob": "实现阶段生成任务",
    "source event": "来源事件", "content hash": "内容哈希",
    "helper surface": "辅助接口", "helper/interface": "辅助函数/接口",
    "reserved bits": "保留位", "lower configured MSS index": "低于配置的 MSS 索引",
    "initial ACK": "首次 ACK", "session reuse": "会话复用",
    "iCode/iPipe": "代码提交/流水线验证",
    "producer-block": "生成任务阻塞", "ProducerJob": "生成任务",
    "WorkerDriver": "工作流驱动器", "IMPLEMENT": "实现阶段",
    "Plan": "任务计划", "patch": "补丁", "commit": "提交",
    "artifact": "产物", "borrowed": "借用的", "reuse": "复用",
    "fixture": "测试夹具", "helper": "辅助函数", "interface": "接口",
    "validator rejection": "校验器拒绝", "validator": "校验器",
    "positive control": "正向对照", "negative fixture": "负向测试夹具",
    "control": "对照", "negative": "负向", "positive": "正向",
    "cookie layout": "Cookie 布局", "cookie": "Cookie",
    "stale": "过期", "sequence": "序列号", "hash": "哈希",
    "tuple": "连接元组", "time-window": "时间窗口",
    "sequence-low": "序列号低位", "arbitrary": "任意构造的",
}
_MIXED_DIAGNOSIS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(term) for term in sorted(_MIXED_DIAGNOSIS_TERMS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])", re.IGNORECASE
)
_MIXED_DIAGNOSIS_LOOKUP = {key.lower(): value for key, value in _MIXED_DIAGNOSIS_TERMS.items()}
_DIAGNOSIS_INLINE_IDENTIFIERS = re.compile(
    r"(?<![A-Za-z0-9_])(?:R[0-9]+|G[0-9]+|QA|MSS|RTT|ACK|SYN|"
    r"Cookie|wscale|svc|ack|iPipe|iCode)(?![A-Za-z0-9_])"
)

_TECHNICAL_TOKEN = re.compile(
    r"`[^`\n]+`|(?<![A-Za-z0-9_\u4e00-\u9fff])/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
    r"|(?<![A-Za-z0-9_])[0-9a-f]{7,64}(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|(?<![A-Za-z0-9_])[A-Z]+-\d+(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.:-]+)+"
)

# Four or more English words in a row is a sentence an approver would have to read in
# English; fewer is an identifier or a term inside Chinese prose.
_ENGLISH_PROSE = re.compile(r"[A-Za-z][a-z]+(?:[\s,;:.'()-]+[A-Za-z][a-z]+){3,}")

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
        return str(value) if isinstance(value, (int, float)) else "-"
    if value in _DIAGNOSIS_ENUMS:
        return _DIAGNOSIS_ENUMS[value]
    if value in _DIAGNOSIS_PHRASES:
        return _DIAGNOSIS_PHRASES[value]
    if value in _BGW_1995_DIAGNOSIS_TRANSLATIONS:
        return _BGW_1995_DIAGNOSIS_TRANSLATIONS[value]
    if value in _DISPLAY_ENUMS:
        return _DISPLAY_ENUMS[value]
    if value == "PLAN_INCOMPLETE: approved task plan requires removed global/VS c2s_mss exact-index equality":
        return "计划不完整：已批准计划仍包含已移除的全局与 VS c2s_mss 严格索引相等要求"
    for pattern, translation in _DIAGNOSIS_SAFE_PATTERNS:
        if pattern.fullmatch(value):
            return translation
    structural = re.match(
        r"^(EXPECTED versus OBSERVED|SOURCE REPRODUCTION|LOG EVIDENCE|"
        r"MINIMAL VERIFICATION|REPAIR PLAN|REPRODUCTION|OBSERVED|EXPECTED)\s*[:：]\s*(.+)$",
        value,
        flags=re.DOTALL,
    )
    if structural:
        label = _DIAGNOSIS_STRUCTURAL_LABELS[structural.group(1)]
        translated = _diagnosis_text(structural.group(2).strip())
        return f"{label}：{translated}"
    if re.search(r"[\u4e00-\u9fff]", value):
        # Translate only prose; never rewrite code spans, paths or hashes.
        parts = re.split(r"(`[^`\n]+`|/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", value)
        value = "".join(
            part if index % 2 else _MIXED_DIAGNOSIS_PATTERN.sub(
                # PLAN/SPEC/IMPLEMENT in capitals name a phase and stay as written.
                lambda match: match.group() if match.group().isupper()
                else _MIXED_DIAGNOSIS_LOOKUP[match.group().lower()], part
            )
            for index, part in enumerate(parts)
        )
    # A Chinese character must not let a whole English sentence bypass the guard, but
    # identifiers inside Chinese prose (NAT64, PLAN, nortt, G5) are not a sentence.
    # Explicit code spans are treated as opaque technical material, not translated.
    remainder = _TECHNICAL_TOKEN.sub("", value)
    if re.search(r"[\u4e00-\u9fff]", value):
        remainder = _DIAGNOSIS_INLINE_IDENTIFIERS.sub("", remainder)
    if not _ENGLISH_PROSE.search(remainder):
        return _TECHNICAL_TOKEN.sub(
            lambda match: "`" + match.group().strip("`") + "`", value
        )
    # Never put unknown English in an approval body.  Keep only unambiguous technical
    # tokens so paths, hashes, commands and symbols remain auditable without guessing
    # at the meaning of the surrounding prose.
    tokens = list(dict.fromkeys(_TECHNICAL_TOKEN.findall(value)))
    token_text = "、".join("`" + token.strip("`") + "`" for token in tokens)
    suffix = f" 技术标识：{token_text}" if token_text else ""
    return f"材料待翻译：该诊断文本尚无安全的中文译文，请补齐中文说明后再审批；原文保留在材料包中。{suffix}"


def _signature_text(value: Any) -> str:
    """A failure signature is an identity to compare, not prose to translate."""
    if not isinstance(value, str) or not value.strip():
        return "-"
    for table in (_DIAGNOSIS_ENUMS, _DIAGNOSIS_PHRASES, _BGW_1995_DIAGNOSIS_TRANSLATIONS):
        if value in table:
            return table[value]
    return "`" + " ".join(value.split()).replace("`", "")[:300] + "`"


def _diagnosis_summary(content: dict[str, Any]) -> list[str]:
    lines = [
        f"诊断分类 {_diagnosis_text(content.get('classification'))}",
        f"证据状态 {_diagnosis_text(content.get('evidence_state'))}",
        *([f"修复范围 {_diagnosis_text(content['repair_scope'])}"] if content.get("repair_scope") else []),
        f"后续路线 {_diagnosis_text(content.get('route'))}",
        f"失败签名 {_signature_text(content.get('failure_signature'))}",
    ]
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
    baselines = content.get("unchanged_baselines")
    if isinstance(baselines, list):
        for item in baselines:
            if isinstance(item, dict):
                rows.append(
                    f"未变更仓库 {_text(item.get('module'))}，分支 {_text(item.get('branch'))}，"
                    f"基线版本 {_short(item.get('revision'))}（不新建 CR、不触发流水线，"
                    "沿用该版本已发布的产物）"
                )
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
    # Generic artifact summaries also use this helper. Diagnosis fail-closed
    # rendering is deliberately scoped to _diagnosis_text, not unrelated gates.
    text = str(value)
    return _clip(_DIAGNOSIS_ENUMS.get(text, _DISPLAY_ENUMS.get(text, text)))


def _short(value: Any) -> str:
    return f"{value[:12]}…" if isinstance(value, str) and len(value) > 12 else _text(value)


def _clip(value: str, limit: int = 60) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"
