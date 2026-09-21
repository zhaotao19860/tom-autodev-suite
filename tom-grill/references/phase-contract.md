# Grill Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Grill and not already there.

Grill's job: close the human decisions that block a reliable Spec, and record the acceptance criteria the requirement owner commits to.

Phase-specific rules:

- An `acceptance_delta` id that already exists in the snapshot is a redefinition and fails as `ACCEPTANCE_DELTA_CONFLICT`.
- `acceptance-candidates` results are advisory drafts, never part of any phase action identity or approval `input_hash` — a live document fetch would make those unstable. A candidate becomes an `acceptance_delta` entry only after the requirement owner confirms it, with the returned document id and content hash recorded as that entry's `evidence`.
