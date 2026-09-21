# Plan Phase Contract

Model output follows the shared [producer contract](../../tom-autodev/references/producer-contract.md) and the current action's content schema. The worker owns the envelope, persistence, gates and transitions. [Phase protocol](../../tom-autodev/references/phase-protocol.md) and [phase artifacts](../../tom-autodev/references/phase-artifacts.md) describe that archival/controller boundary; this file keeps only phase-specific rules.

Plan's job: turn one approved Task DAG node into an executable per-task plan (exact repos/files/symbols, ordered edits, test IDs/fixtures/assertions, iPipe parameters, checkbox steps) another agent can follow without rediscovering architecture. Resolve ambiguity before G4; on a Spec conflict return `SPEC_CONFLICT` to `tom-spec`. Field list and gate are in `../SKILL.md`.
