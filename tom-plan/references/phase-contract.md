# Plan Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Plan and not already there.

Plan's job: turn one approved Task DAG node into an executable per-task plan (exact repos/files/symbols, ordered edits, test IDs/fixtures/assertions, iPipe parameters, checkbox steps) another agent can follow without rediscovering architecture. Resolve ambiguity before G4; on a Spec conflict return `SPEC_CONFLICT` to `tom-spec`. Field list and gate are in `../SKILL.md`.
