# Tasks Phase Contract

Model output follows the shared [producer contract](../../tom-autodev/references/producer-contract.md) and the current action's content schema. The worker owns the envelope, persistence, gates and transitions. [Phase protocol](../../tom-autodev/references/phase-protocol.md) and [phase artifacts](../../tom-autodev/references/phase-artifacts.md) describe that archival/controller boundary; this file keeps only phase-specific rules.

Tasks' job: decompose an approved Spec into an acyclic DAG of independently verifiable end-to-end behavior slices.

Phase-specific rule:

- Each node carries `business_module`, a required field naming exactly one registered business repo; WORKSPACE binds the task to that repo, so a cross-repo requirement never cuts a second repo's task against the first repo's worktree.
