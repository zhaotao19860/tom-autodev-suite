# Review Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Review and not already there.

Review's job: produce an independent dual-axis (Standards + Spec) `review` verdict over the fixed change set, with each finding classified `CONFIRMED` / `REJECTED_WITH_REASON` / `NEEDS_CLARIFICATION`. Review carries no approval and cannot submit; only a dual-axis `PASS` lets the parent request the hash-bound G7. Heuristics, review method, finding reception, and verdict rules are in `../SKILL.md`.
