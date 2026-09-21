# Spec Phase Contract

Model output follows the shared [producer contract](../../tom-autodev/references/producer-contract.md) and the current action's content schema. The worker owns the envelope, persistence, gates and transitions. [Phase protocol](../../tom-autodev/references/phase-protocol.md) and [phase artifacts](../../tom-autodev/references/phase-artifacts.md) describe that archival/controller boundary; this file keeps only phase-specific rules.

Spec's job: convert closed requirement decisions into a versioned, reviewable behavioral contract (behaviors, test interface, environment, acceptance traceability) without embedding implementation code. Section order and completion gate are in `../SKILL.md`.
