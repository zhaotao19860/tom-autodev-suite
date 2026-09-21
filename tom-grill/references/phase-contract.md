# Grill Phase Contract

Model output follows the shared [producer contract](../../tom-autodev/references/producer-contract.md) and the current action's content schema. The worker owns the envelope, persistence, gates and transitions. [Phase protocol](../../tom-autodev/references/phase-protocol.md) and [phase artifacts](../../tom-autodev/references/phase-artifacts.md) describe that archival/controller boundary; this file keeps only phase-specific rules.

Grill's job: close the human decisions that block a reliable Spec, and record the acceptance criteria the requirement owner commits to.

Phase-specific rules:

- An `acceptance_delta` id that already exists in the snapshot is a redefinition and fails as `ACCEPTANCE_DELTA_CONFLICT`.
- `acceptance-candidates` results are advisory drafts, never part of any phase action identity or approval `input_hash` — a live document fetch would make those unstable. A candidate becomes an `acceptance_delta` entry only after the requirement owner confirms it, with the returned document id and content hash recorded as that entry's `evidence`.
