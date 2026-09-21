# Tasks Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Tasks and not already there.

Tasks' job: decompose an approved Spec into an acyclic DAG of independently verifiable end-to-end behavior slices.

Phase-specific rule:

- Each node carries `business_module`, a required field naming exactly one registered business repo; WORKSPACE binds the task to that repo, so a cross-repo requirement never cuts a second repo's task against the first repo's worktree.
