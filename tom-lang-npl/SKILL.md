---
name: tom-lang-npl
description: Use when tom-autodev generates, reviews, or designs tests for NPL source such as parser_node, logical_table, function, bus, headers, or special functions.
---

# Tom NPL

## Scope

Provide NPL language rules and test/review guidance for an approved Spec and Task Plan. Keep chip generation limits, XFlow repository topology, knowledge paths, and iPipe profiles in `tom-project-xflow`.

## Generation Rules

- Read `references/npl-core-rules.md` before changing constructs, fields, headers, logical buses, tables, functions, or control flow.
- Use `references/npl-idioms.md` for language construction patterns (feature-gating wrapper, strength arbitration, chip-conditional compilation, flex-editor zone order, bus model, critical constraints).
- Preserve existing NPL naming, stage/bus contracts, field widths, valid/invalid transitions, and externally observable error/drop behavior.
- Use precise source search and current project precedents; do not infer semantics from an unrelated example.
- Design independent product tests at the highest stable external boundary with deterministic packet/input fixtures and explicit expected output.
- Treat compiler logs, chip limits, and tool versions as iPipe evidence, not local facts.

## Error and Review Rules

Classify failures as source syntax/semantic, resource or chip constraint, test contract, environment, pipeline, or revision mismatch before proposing repair. Use `references/error-patterns.md`; confirmed code/test causes return through `tom-diagnose` and a new Task Plan. For compile-failure fix/triage detail (nlc front-end vs xfc back-end stages, the 6 pitfalls, `add_header` tap point, and the build.sh false-success trap) use `references/npl-compile-diagnostics.md`. The canonical NPL language references (spec, coding guidelines, compilation-fixup examples) live in `references/npl-docs/`.

The mandatory checklist in `references/npl-core-rules.md` covers construct legality, field/bus/stage contracts, malformed/drop behavior, external fixtures, and traceability. Pair every NPL source change with an independent product-test change and remote evidence; do not infer chip limits or expected output from memory.

The Mac control plane must not call Docker, NCS, NPL compiler, simulator, unit, regression, or integration commands. Do not modify read-only official examples. Return language decisions to `tom-plan`, `tom-implement`, or `tom-review`; do not call iCode/iPipe directly.

**REQUIRED PARENT:** Load `tom-project-xflow` for project-specific rules before implementation.
