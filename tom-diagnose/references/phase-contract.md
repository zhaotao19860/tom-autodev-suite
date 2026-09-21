# Diagnose Phase Contract

The authoritative contract (inputs / outputs / gate / schema / failure routing) lives in [`../../tom-autodev/references/phase-protocol.md`](../../tom-autodev/references/phase-protocol.md) and [`phase-artifacts.md`](../../tom-autodev/references/phase-artifacts.md); this file records only what is specific to Diagnose and not already there.

Diagnose's job: from a frozen failure bundle, produce one falsifiable root-cause hypothesis with reproduction from remote evidence, comparison to a passing revision, failure class, minimal iPipe verification, confidence, and route. Environment failures never create business patches. Procedure, repair budget, and gate are in `../SKILL.md`.
