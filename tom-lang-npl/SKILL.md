---
name: tom-lang-npl
description: Use when tom-autodev generates, reviews, or designs tests for NPL source such as parser_node, logical_table, function, bus, headers, or special functions.
---

# Tom NPL

## Scope

Provide NPL language rules and test/review guidance for an approved Spec and Task Plan. Chip limits, repository topology, knowledge paths, and iPipe configuration belong to the caller's project profile; XFlow uses `tom-project-xflow` for that role.

## Generation Rules

- Read `references/npl-core-rules.md` before changing constructs, fields, headers, logical buses, tables, functions, or control flow.
- Use `references/npl-idioms.md` for construction patterns; verify the project's wrapper, strength, mapping, and editor conventions before adopting them.
- Preserve existing NPL naming, stage/bus contracts, field widths, valid/invalid transitions, and externally observable error/drop behavior.
- Use precise source search and current project precedents; do not infer semantics from an unrelated example.
- Design independent product tests at the highest stable external boundary with deterministic packet/input fixtures and explicit expected output.
- Treat compiler logs, chip limits, and tool versions as iPipe evidence, not local facts. Select the smallest relevant section through `references/docs-index.md`; use `references/sources.md` to distinguish normative rules from project precedent and historical observation.

## Error and Review Rules

Classify failures by evidence from the emitting stage before proposing repair. Use `references/error-patterns.md`; confirmed code/test causes return through `tom-diagnose` and a new Task Plan. For nlc/xfc/SDKLT evidence, conditional tap-point and packing patterns, and the false-success trap, use `references/npl-compile-diagnostics.md`. The bundled language specification, coding guidelines, and compiler cases are selected through `references/docs-index.md`.

The mandatory checklist in `references/npl-core-rules.md` covers construct legality, field/bus/stage contracts, malformed/drop behavior, external fixtures, and traceability. Pair every NPL source change with an independent product-test change and remote evidence; do not infer chip limits or expected output from memory.

The Mac control plane must not call Docker, NCS, NPL compiler, simulator, unit, regression, or integration commands. Do not modify read-only official examples. Return language decisions to `tom-plan`, `tom-implement`, or `tom-review`; do not call iCode/iPipe directly.

The caller's project profile selects chip, repository, environment, and delivery policy. Return missing project facts to that caller; this language skill does not force-load a project skill or assume every NPL task is XFlow.
