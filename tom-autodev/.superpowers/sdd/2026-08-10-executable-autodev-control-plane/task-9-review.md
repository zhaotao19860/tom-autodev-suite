# Task 9 Independent Review

## Verdicts

- **Specification: FAIL** - no Critical finding, but I1-I3 leave required phase/gate and failure-routing contracts incomplete.
- **Standards/content quality: FAIL** - links, hashes, host boundary, and language/project checklists are otherwise sound, but I2 creates a cross-document vocabulary conflict and I1/I3 make the operating contract ambiguous.

## Critical

None.

## Important

### I1 - The documented Phase Protocol omits controller phases and their gates

`tom-autodev/references/phase-protocol.md:3` says every phase exchanges an immutable `ArtifactEnvelope`, but its only phase table (`:14-22`) stops at the seven child-skill phases. It does not define the required input, output/receipt, completion predicate, approval gate, or stop behavior for `WORKSPACE`, `SUBMIT`, `IPIPE`, `RELEASE`, and terminal outcomes even though the binding Task 9 contract names those phases. Consequently G8 and G9 are absent from the protocol, while G10 appears only in prose in `tom-autodev/SKILL.md:62-66`; the end-to-end contract is not executable from the Skill documentation alone.

Required repair: either extend the protocol with explicit controller-phase contracts (including WorkspaceGate evidence, G7 submission receipt, G8 rerun/manual-continuation evidence, iPipe evidence schema, G9 release verification, terminal outcomes, and stop predicates) or clearly separate and link a complete controller protocol. Keep all adapter calls and G0-G10 ownership in the parent.

### I2 - NPL failure classes do not map to the canonical repair taxonomy

`tom-lang-npl/references/error-patterns.md:3-10` emits language-specific classes such as `Source syntax/semantic`, `Resource/chip constraint`, `Test contract`, and `Environment`. The parent contract and `tom-autodev/references/failure-taxonomy.md:3-15` route only canonical values such as `CODE_FAILURE`, `TEST_FAILURE`, `ENV_UNSATISFIED`, `ENV_TRANSIENT`, `PIPELINE_TRANSIENT`, and `REVISION_MISMATCH`. There is no normative mapping for a compiler resource/capacity failure, so the same evidence can be treated as a repairable source defect or a non-repairable environment problem. That ambiguity directly affects `tom-diagnose`, repair-budget policy, and development/test owner routing.

Required repair: make the table emit one canonical class for every row, with a separate NPL subtype if needed. Define evidence-based disambiguation for chip/resource exhaustion versus runner/environment capacity and ensure each route matches the parent taxonomy.

### I3 - The Review phase contract does not explicitly name its approval gate or complete stop predicates

The Task 9 requirement says every phase contract must name its approval gate and stop conditions. `tom-review/references/phase-contract.md:3` says only that dual-axis PASS "reaches G7"; it does not state that G7 approval is required and bound to the reviewed Change Set. It also omits explicit stops for provider `INCOMPLETE`, `NEEDS_CLARIFICATION`, stale/hash-mismatched baseline, and blocking confirmed findings. `tom-review/SKILL.md:33-37` describes verdict routing, but the compact phase contract still fails the required standalone contract shape, and `tom-autodev/references/phase-protocol.md:21` labels the gate only as `G7 path`.

Required repair: state that Review itself has no approval-bearing result, dual-axis PASS is a prerequisite for a separately hash-bound parent-owned G7 iCode-submission approval, and enumerate the non-PASS stop/route predicates. Use one wording consistently in the Skill, phase contract, and protocol table.

## Minor

None.

## Confirmed Strengths

- All SHA-256 values listed in `task-9-report.md` match the reviewed files.
- All Markdown local links in the scoped Skills/references resolve.
- No scoped trigger/workflow/reference contains Codex or standalone `tom-autorelease`.
- The Comate-only boundary and Mac/iPipe execution split are explicit and consistent; no wording authorizes local BGW/XFlow build, test, regression, integration, Docker, NCS, simulator, or release execution.
- Child Skills consistently return artifacts to the parent; iCafe/KU/iCode/iPipe/Infoflow adapters remain parent-owned.
- C/C++ source/test review coverage is concrete. NPL construct, field, stage, fixture, and external-boundary checks are concrete apart from I2.
- BGW/XFlow Skills require explicit independent test repositories, pinned environments/toolchains, graph freshness, and remote stages, and setup refuses inference of repositories, pipelines, tool limits, or environments.

## Review Method

Read the Task 9 brief, implementation report, ledger, every scoped Skill/reference, linked parent contracts, and the relevant canonical failure/gate references. Per constraints, no live service was called and no BGW/XFlow project command was run. The implementation report's quick-validation transcript was treated as evidence; this review independently checked content, hashes, and links without editing any Skill.
