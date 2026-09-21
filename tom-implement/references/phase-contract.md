# Implement Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Implement and not already there.

Implement's job: execute the exact G4-approved Task Plan in owned worktrees to produce one atomic `change-set` (business + independent product-test patches, complete diff, deviations, hashes) for EvidenceGate and G5. The Mac control plane only generates and inspects source; all execution belongs to iPipe. Procedure and hard boundary are in `../SKILL.md`.
