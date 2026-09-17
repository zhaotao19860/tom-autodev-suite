"""The single source of truth for the tom-autodev workflow.

Before this module the workflow's shape lived in at least eight places keyed by
phase/transition — `phase_protocol._PHASES`/`_CONTROLLERS`/`_TITLE`/
`_TASK_SCOPED_TITLES`/`_allowed_side_effects`, `transition_policy.ALLOWED_TRANSITIONS`/
`TERMINAL_STATES`/`failure_target`, `evidence_policy.REQUIREMENTS`/
`TRANSITION_REQUIREMENTS`, and `approval_summary._GATES` — kept in step by hand
(the "keep these in step with `_PHASES`" comment was the tell). This module states
each fact once; `tests/test_workflow_spec.py` binds every legacy structure to it, so
a divergence is a failing test rather than a silent drift.

Two gate senses are kept distinct rather than merged:
- `entry_gate`  — the approval that must already be APPROVE to act on this state
  (what `evidence_policy.REQUIREMENTS[state].approval_gate` enforces on entry).
- `output_gate` — the gate on the artifact this state itself produces, shown on the
  approval card (`phase_protocol._PHASES[state]["gate"]`, emitted as
  `required_human_gate`). e.g. IMPLEMENT is entered on G4 (the plan) and its
  change-set output is gated by G5; the two are different gates, not a drift.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

WORKFLOW_VERSION = "workflow-spec-v1"

# Side effects a skill-backed phase (and INTAKE, which is a controller by name but is
# registered as a phase) may perform. Controller states carry their own narrower lists.
_SKILL_SIDE_EFFECTS: tuple[str, ...] = (
    "artifact.write",
    "knowledge.publish",
    "icafe.comment",
    "state.transition",
)


@dataclass(frozen=True)
class StateSpec:
    """Everything the control plane needs to know about one workflow state."""

    transitions: frozenset[str]
    terminal: bool = False
    # "phase" -> reconstructed into phase_protocol._PHASES; "controller" -> _CONTROLLERS;
    # None -> a transition-graph-only state (blocked/terminal) with no phase definition.
    registry: str | None = None
    skill: str | None = None
    controller: str | None = None
    schema: str | None = None
    entry_gate: str | None = None
    output_gate: str | None = None
    target: str | None = None
    predecessor: Any = None
    envelope_revisions: bool = False
    title: str | None = None
    task_scoped: bool = False
    side_effects: tuple[str, ...] = ()
    entry_artifacts: tuple[str, ...] = ()
    evidence_requires_revisions: bool = False
    evidence_requires_environment: bool = False


# placeholder-states
# The non-obvious transition edges and why they exist (moved here with the graph):
#   WORKSPACE -> TASKS: a DAG defect is only provable at bind time (one business repo
#     per task), so a node spanning two repos is re-cut rather than discarding a run
#     with approved phases. Re-entry needs the G2 spec as evidence and a fresh G3.
#   REVIEW -> DIAGNOSE: the CR is where second opinions (小码哥 + human) arrive after
#     SUBMIT; a confirmed defect is root-caused before anything is repaired, so it
#     leaves via DIAGNOSE -> PLAN/SPEC and returns as a new revision on the same CR.
#   REVIEW/SUBMIT -> WORKSPACE: a PASS submits that task immediately; the CR can land
#     while later DAG nodes are open, so the frontier rebinds instead of running IPIPE
#     over a half-written requirement.
#   DIAGNOSE -> PLAN: only for a CODE_ONLY repair (spec + DAG still hold), decided by
#     phase_protocol._completion_target(); a spec/scope repair re-enters at SPEC.
STATES: dict[str, StateSpec] = {
    "INTAKE": StateSpec(
        registry="phase", controller="intake", schema="requirement-snapshot",
        output_gate="G0", target="GRILL", title="00-requirement-snapshot",
        side_effects=_SKILL_SIDE_EFFECTS, transitions=frozenset({"GRILL"}),
    ),
    "GRILL": StateSpec(
        registry="phase", skill="tom-grill", schema="decision-log",
        entry_gate="G0", output_gate="G1", target="SPEC", predecessor="INTAKE",
        title="01-grill", side_effects=_SKILL_SIDE_EFFECTS,
        entry_artifacts=("requirement-snapshot", "collaboration-session"),
        transitions=frozenset({"SPEC"}),
    ),
    "SPEC": StateSpec(
        registry="phase", skill="tom-spec", schema="spec",
        entry_gate="G1", output_gate="G2", target="TASKS", predecessor="GRILL",
        title="02-spec", side_effects=_SKILL_SIDE_EFFECTS, entry_artifacts=("grill",),
        transitions=frozenset({"TASKS"}),
    ),
    "TASKS": StateSpec(
        registry="phase", skill="tom-tasks", schema="task-dag",
        entry_gate="G2", output_gate="G3", target="WORKSPACE", predecessor="SPEC",
        title="03-tasks", side_effects=_SKILL_SIDE_EFFECTS, entry_artifacts=("spec",),
        transitions=frozenset({"WORKSPACE"}),
    ),
    "WORKSPACE": StateSpec(
        registry="controller", controller="workspace",
        entry_gate="G3", output_gate="G4", target="PLAN", predecessor="TASKS",
        side_effects=("workspace.inspect", "workspace.create", "state.transition"),
        entry_artifacts=("task-dag",), transitions=frozenset({"PLAN", "TASKS"}),
    ),
    "PLAN": StateSpec(
        registry="phase", skill="tom-plan", schema="task-plan",
        entry_gate="G4", output_gate="G4", target="IMPLEMENT", predecessor="TASKS",
        envelope_revisions=True, title="04-task-plan", task_scoped=True,
        side_effects=_SKILL_SIDE_EFFECTS, entry_artifacts=("workspace", "task-plan"),
        transitions=frozenset({"IMPLEMENT"}),
    ),
    "IMPLEMENT": StateSpec(
        registry="phase", skill="tom-implement", schema="change-set",
        entry_gate="G4", output_gate="G5", target="REVIEW", predecessor="PLAN",
        envelope_revisions=True, title="05-change-set", task_scoped=True,
        side_effects=_SKILL_SIDE_EFFECTS, entry_artifacts=("task-plan",),
        transitions=frozenset({"REVIEW"}),
    ),
    # more-states
    "REVIEW": StateSpec(
        registry="phase", skill="tom-review", schema="review",
        entry_gate="G5", output_gate=None, target="SUBMIT", predecessor="IMPLEMENT",
        envelope_revisions=True, title="06-review", task_scoped=True,
        side_effects=_SKILL_SIDE_EFFECTS, entry_artifacts=("change-set",),
        transitions=frozenset({"WORKSPACE", "SUBMIT", "DIAGNOSE", "STOPPED"}),
    ),
    "SUBMIT": StateSpec(
        registry="controller", controller="submit",
        entry_gate="G7", output_gate="G7", target="IPIPE", predecessor="REVIEW",
        side_effects=("icode.submit", "state.transition"), entry_artifacts=("review",),
        transitions=frozenset({"IPIPE", "DIAGNOSE", "WORKSPACE"}),
    ),
    "IPIPE": StateSpec(
        registry="controller", controller="ipipe", entry_gate="G7", envelope_revisions=True,
        title="08-ipipe-evidence",
        side_effects=("ipipe.trigger", "ipipe.monitor", "artifact.write", "knowledge.publish", "icafe.comment"),
        entry_artifacts=("submission",),
        transitions=frozenset({"RELEASE", "DIAGNOSE", "ENVIRONMENT_BLOCKED"}),
    ),
    "DIAGNOSE": StateSpec(
        registry="phase", skill="tom-diagnose", schema="diagnosis", output_gate="G6",
        predecessor=("REVIEW", "IPIPE", "IMPLEMENT"), envelope_revisions=True,
        title="07-diagnosis", side_effects=_SKILL_SIDE_EFFECTS,
        transitions=frozenset({"SPEC", "PLAN", "ARCHITECTURE_REVIEW", "STOPPED"}),
    ),
    "RELEASE": StateSpec(
        registry="controller", controller="release",
        entry_gate="G9", output_gate="G9", predecessor="IPIPE", envelope_revisions=True,
        side_effects=("release.verify", "artifact.write", "knowledge.publish", "icafe.comment", "state.transition"),
        entry_artifacts=("ipipe-evidence",),
        evidence_requires_revisions=True, evidence_requires_environment=True,
        transitions=frozenset({"RELEASE_SUCCESS", "DIAGNOSE", "ENVIRONMENT_BLOCKED"}),
    ),
    "ARCHITECTURE_REVIEW": StateSpec(transitions=frozenset({"GRILL", "SPEC", "TASKS"})),
    "ENVIRONMENT_BLOCKED": StateSpec(transitions=frozenset({"IPIPE", "STOPPED"})),
    "RELEASE_SUCCESS": StateSpec(
        terminal=True, entry_gate="G9", entry_artifacts=("release-evidence",),
        evidence_requires_revisions=True, evidence_requires_environment=True,
        transitions=frozenset(),
    ),
    "STOPPED": StateSpec(terminal=True, transitions=frozenset()),
}


# Evidence keyed on a transition rather than a target state (evidence_policy
# .TRANSITION_REQUIREMENTS): (from, to) -> (entry_artifacts, gate_or_None).
TRANSITION_REQUIREMENTS: dict[tuple[str, str], tuple[tuple[str, ...], str | None]] = {
    ("REVIEW", "WORKSPACE"): (("review",), None),
    ("REVIEW", "STOPPED"): (("review",), None),
    ("DIAGNOSE", "SPEC"): (("failure-bundle",), "G6"),
    ("DIAGNOSE", "ARCHITECTURE_REVIEW"): (("failure-bundle",), "G6"),
    ("ENVIRONMENT_BLOCKED", "IPIPE"): (("submission",), "G8"),
}

# Human-readable (subject, effect) per gate, for approval cards.
GATE_LABELS: dict[str, tuple[str, str]] = {
    "G0": ("需求快照与协作绑定（群名、成员、角色）", "建协作群，进入 GRILL 澄清"),
    "G1": ("GRILL 决策日志：澄清结论、决策、验收点", "发布决策日志，进入 SPEC 起草"),
    "G2": ("Spec：行为、验收场景、测试接口、环境要求", "发布 Spec，进入 TASKS 拆解"),
    "G3": ("任务 DAG 与验收点覆盖", "创建工作区，进入 PLAN"),
    "G4": ("单个任务的实现计划", "按计划生成业务代码与测试代码"),
    "G5": ("候选改动集：业务 diff 与测试 diff", "进入标准与 Spec 双轴 Review"),
    "G6": ("诊断结论与修复方向", "按修复方向执行修复"),
    "G7": ("提交 iCode 并触发 iPipe 的固定版本", "提交 CR，触发允许的流水线"),
    "G8": ("重跑失败的流水线阶段", "按当前远程证据重跑该阶段"),
    "G9": ("正式发布", "执行发布阶段"),
    "G10": ("控制面优化建议", "应用优化建议"),
}

# Failure reason -> next state (transition_policy.failure_target); default STOPPED.
FAILURE_TARGETS: dict[str, str] = {
    "REQUIREMENT_CHANGED": "GRILL",
    "ENV_UNSATISFIED": "ENVIRONMENT_BLOCKED",
    "ENV_TRANSIENT": "ENVIRONMENT_BLOCKED",
    "CODE_FAILURE": "DIAGNOSE",
    "TEST_FAILURE": "DIAGNOSE",
    "REVIEW_FAILED": "DIAGNOSE",
    "RELEASE_FAILED": "DIAGNOSE",
}
_FAILURE_DEFAULT = "STOPPED"


@dataclass(frozen=True)
class ChangeClass:
    """How a change class routes through the phases.

    `phase_modes[state]` is one of:
      - "full"   : the skill/model produces the artifact and its normal gate applies
                   (the default for any state not listed).
      - "auto"   : the controller produces the artifact deterministically — no model,
                   no gate (the state's gate is waived).
      - "merged" : the artifact is produced together with a sibling state in one model
                   step under one gate (kept), and the sibling is "auto".
    Only the clarify/design front (GRILL/SPEC/TASKS) varies; PLAN/IMPLEMENT/REVIEW and
    every code/side-effect gate (G4/G5/G7/G9) are always "full".
    """

    name: str
    phase_modes: dict[str, str]


CHANGE_CLASSES: dict[str, ChangeClass] = {
    # The full path exactly as today: every phase is skill-produced with its own gate.
    "standard": ChangeClass("standard", phase_modes={}),
    # A small change: GRILL auto-derived (acceptance already present), SPEC produced
    # together with the task-dag in one gated step, TASKS auto-derived from that dag.
    "express": ChangeClass("express", phase_modes={"GRILL": "auto", "SPEC": "merged", "TASKS": "auto"}),
}
DEFAULT_CHANGE_CLASS = "standard"

# iCafe card types that deterministically SUGGEST the express class (owner confirms /
# overrides at G0). Everything else — including an unknown/missing type — defaults to
# standard, so the safe full path is the fallback.
_EXPRESS_CARD_TYPES = frozenset({"bug", "缺陷", "缺陷修复", "fix", "hotfix", "defect"})


def phase_mode(change_class: str, state: str) -> str:
    """Production mode of a state for a change class; "full" unless the class overrides."""
    spec = CHANGE_CLASSES.get(change_class) or CHANGE_CLASSES[DEFAULT_CHANGE_CLASS]
    return spec.phase_modes.get(state, "full")


def waived_gates(change_class: str) -> frozenset[str]:
    """Gates a class waives = the OUTPUT gate of each "auto" state.

    Only the output gate (the human sign-off on that phase's own artifact) is waived; the
    entry gate is the predecessor's output and stays — notably GRILL's entry G0, the
    collaboration/class-declaration approval, is never waived.
    """
    modes = (CHANGE_CLASSES.get(change_class) or CHANGE_CLASSES[DEFAULT_CHANGE_CLASS]).phase_modes
    gates: set[str] = set()
    for state, mode in modes.items():
        if mode == "auto":
            spec = STATES.get(state)
            if spec is not None and spec.output_gate:
                gates.add(spec.output_gate)
    return frozenset(gates)


def classify_change(snapshot: Any, override: str | None = None) -> str:
    """Suggested change class from the requirement snapshot; an explicit override wins.

    Deterministic: a known express-ish card type suggests express, everything else is
    standard. The G0 approver confirms or overrides — nothing here decides on its own.
    """
    if isinstance(override, str) and override in CHANGE_CLASSES:
        return override
    card_type = ""
    if isinstance(snapshot, dict):
        card_type = str(snapshot.get("type") or "").strip().lower()
    if card_type and any(keyword in card_type for keyword in _EXPRESS_CARD_TYPES):
        return "express"
    return DEFAULT_CHANGE_CLASS


# helpers
def allowed_transitions() -> dict[str, set[str]]:
    """Reconstruct transition_policy.ALLOWED_TRANSITIONS."""
    return {state: set(spec.transitions) for state, spec in STATES.items()}


def terminal_states() -> frozenset[str]:
    return frozenset(state for state, spec in STATES.items() if spec.terminal)


def failure_target(reason_code: str) -> str:
    return FAILURE_TARGETS.get(reason_code, _FAILURE_DEFAULT)


def gate_label(gate: str) -> tuple[str, str]:
    return GATE_LABELS.get(gate, ("", ""))


def _definition(spec: StateSpec) -> dict[str, Any]:
    """Reconstruct one phase_protocol._PHASES/_CONTROLLERS entry from a StateSpec.

    Only the keys the legacy dicts carried are emitted (all reads use .get()), so the
    reconstructed definition behaves identically to the old literal.
    """
    definition: dict[str, Any] = {
        "schema": spec.schema, "gate": spec.output_gate, "target": spec.target,
    }
    if spec.skill is not None:
        definition["skill"] = spec.skill
    if spec.controller is not None:
        definition["controller"] = spec.controller
    if spec.predecessor is not None:
        definition["predecessor"] = spec.predecessor
    if spec.envelope_revisions:
        definition["revisions"] = True
    return definition


def phase_definitions() -> dict[str, dict[str, Any]]:
    return {s: _definition(spec) for s, spec in STATES.items() if spec.registry == "phase"}


def controller_definitions() -> dict[str, dict[str, Any]]:
    return {s: _definition(spec) for s, spec in STATES.items() if spec.registry == "controller"}


def titles() -> dict[str, str]:
    return {s: spec.title for s, spec in STATES.items() if spec.title is not None}


def task_scoped_titles() -> frozenset[str]:
    return frozenset(s for s, spec in STATES.items() if spec.task_scoped)


def canonical_hash() -> str:
    """A stable content hash of the whole spec, for pinning the workflow version."""
    payload = {
        "version": WORKFLOW_VERSION,
        "states": {
            state: {
                "transitions": sorted(spec.transitions),
                "terminal": spec.terminal,
                "registry": spec.registry,
                "skill": spec.skill,
                "controller": spec.controller,
                "schema": spec.schema,
                "entry_gate": spec.entry_gate,
                "output_gate": spec.output_gate,
                "target": spec.target,
                "predecessor": list(spec.predecessor) if isinstance(spec.predecessor, tuple) else spec.predecessor,
                "envelope_revisions": spec.envelope_revisions,
                "title": spec.title,
                "task_scoped": spec.task_scoped,
                "side_effects": list(spec.side_effects),
                "entry_artifacts": list(spec.entry_artifacts),
                "evidence_requires_revisions": spec.evidence_requires_revisions,
                "evidence_requires_environment": spec.evidence_requires_environment,
            }
            for state, spec in sorted(STATES.items())
        },
        "transition_requirements": {
            f"{src}->{dst}": [list(arts), gate]
            for (src, dst), (arts, gate) in sorted(TRANSITION_REQUIREMENTS.items())
        },
        "gate_labels": {gate: list(label) for gate, label in sorted(GATE_LABELS.items())},
        "failure_targets": dict(sorted(FAILURE_TARGETS.items())),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
