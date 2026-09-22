from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import workflow_spec
from approval_ledger import gate_of


@dataclass(frozen=True)
class EvidenceRequirement:
    artifacts: tuple[str, ...] = ()
    approval_gate: str | None = None
    requires_revisions: bool = False
    requires_environment: bool = False


# Derived from the single workflow spec (workflow_spec.py). A state's entry evidence is
# its spec entry_artifacts + entry_gate + evidence_requires_* flags; the generic G0..G10
# actions remain for adapters. Editing a requirement means editing the spec.
REQUIREMENTS: dict[str, EvidenceRequirement] = {
    **{
        state: EvidenceRequirement(
            artifacts=spec.entry_artifacts,
            approval_gate=spec.entry_gate,
            requires_revisions=spec.evidence_requires_revisions,
            requires_environment=spec.evidence_requires_environment,
        )
        for state, spec in workflow_spec.STATES.items()
        if spec.entry_gate or spec.entry_artifacts
    },
    **{f"G{number}": EvidenceRequirement(approval_gate=f"G{number}") for number in range(11)},
}

TRANSITION_REQUIREMENTS: dict[tuple[str, str], EvidenceRequirement] = {
    key: EvidenceRequirement(artifacts=artifacts, approval_gate=gate)
    for key, (artifacts, gate) in workflow_spec.TRANSITION_REQUIREMENTS.items()
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
        and gate_of(approval.get("action")) == gate
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
