from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceRequirement:
    artifacts: tuple[str, ...] = ()
    approval_gate: str | None = None
    requires_revisions: bool = False
    requires_environment: bool = False


# These requirements are owned by the controller. A caller may add evidence
# requirements for a narrower operation, but cannot remove these baseline ones.
REQUIREMENTS: dict[str, EvidenceRequirement] = {
    "GRILL": EvidenceRequirement(("requirement-snapshot", "collaboration-session"), "G0"),
    "SPEC": EvidenceRequirement(("grill",), "G1"),
    "TASKS": EvidenceRequirement(("spec",), "G2"),
    "WORKSPACE": EvidenceRequirement(("task-dag",), "G3"),
    "PLAN": EvidenceRequirement(("workspace", "task-plan"), "G4"),
    "IMPLEMENT": EvidenceRequirement(("task-plan",), "G4"),
    "REVIEW": EvidenceRequirement(("change-set",), "G5"),
    "SUBMIT": EvidenceRequirement(("review",), "G7"),
    "IPIPE": EvidenceRequirement(("submission",), "G7"),
    "RELEASE": EvidenceRequirement(
        ("ipipe-evidence",), "G9", requires_revisions=True, requires_environment=True
    ),
    "RELEASE_SUCCESS": EvidenceRequirement(
        ("release-evidence",), "G9", requires_revisions=True, requires_environment=True
    ),
    # Generic gate actions remain useful to adapters. Every human gate binds
    # an approval record to the canonical input hash.
    **{f"G{number}": EvidenceRequirement(approval_gate=f"G{number}") for number in range(11)},
}

TRANSITION_REQUIREMENTS: dict[tuple[str, str], EvidenceRequirement] = {
    ("REVIEW", "WORKSPACE"): EvidenceRequirement(("review",)),
    ("REVIEW", "STOPPED"): EvidenceRequirement(("review",)),
    ("DIAGNOSE", "SPEC"): EvidenceRequirement(("failure-bundle",), "G6"),
    ("DIAGNOSE", "ARCHITECTURE_REVIEW"): EvidenceRequirement(("failure-bundle",), "G6"),
    ("ENVIRONMENT_BLOCKED", "IPIPE"): EvidenceRequirement(("submission",), "G8"),
}


def requirement_for(action: str, current_state: str | None = None) -> EvidenceRequirement:
    if current_state is not None:
        transition_requirement = TRANSITION_REQUIREMENTS.get((current_state, action))
        if transition_requirement is not None:
            return transition_requirement
    return REQUIREMENTS.get(action, EvidenceRequirement())


def _approval_matches(context: dict[str, Any], gate: str, input_hash: str) -> bool:
    approval_id = context.get("approval_id")
    approval = context.get("approval_record")
    if not isinstance(approval_id, str) or not approval_id or not isinstance(approval, dict):
        return False
    return (
        approval.get("approval_id") == approval_id
        and approval.get("run_id") == context.get("run_id")
        and approval.get("run_id") != "legacy"
        and approval.get("action") == gate
        and approval.get("effective_decision") == "APPROVE"
        and approval.get("input_hash") == input_hash
    )


def _valid_revision_set(value: Any, required_repositories: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    if any(not isinstance(repository, str) or not repository or not isinstance(revision, str) or not revision
           for repository, revision in value.items()):
        return False
    if required_repositories is None:
        return True
    if not isinstance(required_repositories, list) or any(
        not isinstance(repository, str) or not repository for repository in required_repositories
    ):
        return False
    return set(required_repositories).issubset(value)


def missing_evidence(
    action: str, context: dict[str, Any], current_state: str | None = None
) -> tuple[str, list[str]]:
    requirement = requirement_for(action, current_state)
    input_hash = context.get("input_hash")
    approved_input_hash = context.get("approved_input_hash")
    if not isinstance(input_hash, str) or not input_hash or not isinstance(approved_input_hash, str) or not approved_input_hash:
        return "MISSING_INPUT_HASH", []
    if input_hash != approved_input_hash:
        return "INPUT_HASH_MISMATCH", []

    requested = context.get("required_artifacts", [])
    requested_artifacts = requested if isinstance(requested, list) else []
    required = list(dict.fromkeys([*requirement.artifacts, *requested_artifacts]))
    available = context.get("artifacts", [])
    available_set = set(available) if isinstance(available, list) else set()
    missing = [artifact for artifact in required if artifact not in available_set]
    if missing:
        return "MISSING_ARTIFACT", missing

    if requirement.approval_gate and not _approval_matches(context, requirement.approval_gate, input_hash):
        return "APPROVAL_REQUIRED", [requirement.approval_gate]

    if requirement.requires_revisions:
        repo_revisions = context.get("repo_revisions")
        evidence_revisions = context.get("evidence_revisions")
        required_repositories = context.get("required_repositories")
        if not _valid_revision_set(repo_revisions, required_repositories) or not _valid_revision_set(
            evidence_revisions, required_repositories
        ):
            return "MISSING_REVISION_EVIDENCE", []
    if requirement.requires_environment:
        environment = context.get("environment_fingerprint")
        evidence_environment = context.get("evidence_environment_fingerprint")
        if not isinstance(environment, str) or not environment or not isinstance(evidence_environment, str) or not evidence_environment:
            return "MISSING_ENVIRONMENT_EVIDENCE", []

    return "", []
