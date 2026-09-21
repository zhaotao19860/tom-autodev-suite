# Implement Phase Contract

Model output follows the shared [producer contract](../../tom-autodev/references/producer-contract.md) and the current action's content schema. The worker owns the envelope, persistence, gates and transitions. [Phase protocol](../../tom-autodev/references/phase-protocol.md) and [phase artifacts](../../tom-autodev/references/phase-artifacts.md) describe that archival/controller boundary; this file keeps only phase-specific rules.

Implement's job: execute the exact pinned Task Plan under the action's validated approval/mode in owned worktrees to produce one atomic `change-set` (business + independent product-test patches, complete diff, deviations, hashes) for EvidenceGate and G5. The Mac control plane only generates and inspects source; project builds/tests belong to iPipe. Procedure and hard boundary are in `../SKILL.md`.
