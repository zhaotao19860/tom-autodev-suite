# NPL Core Rules

- Confirm construct kind and its legal fields before editing: parser nodes, logical tables, functions, headers, logical bus fields, and special functions have different contracts.
- Match existing project syntax and field naming exactly; use current source precedents before official examples.
- Keep bit widths, offsets, field validity, stage placement, bus direction, and control-flow transitions explicit.
- Preserve unmatched, malformed, drop, trap, mirror, and error behavior unless the approved Spec changes it.
- Do not assume a C/C++ analogy defines NPL semantics. Verify behavior in current NPL sources or approved documentation.
- Test through an external product boundary and trace each assertion to an approved behavior; do not assert generated internal representation unless the Spec makes it observable.

## Language Rules and Target Checks

- NPL has no `return` statement. Wrap early-exit / conditional logic in `if (condition) { ... }` instead of returning.
- Assignment sizing is directional: a wider lvalue receives a zero-extended narrower rvalue; a narrower lvalue than the rvalue is a compile error (NPL 1.5.1, Assignment operators). Validate bus/interface widths and back-end packing separately; do not reject every unequal-width assignment.
- Overlays may target base `bit`/`bit[n]` fields or an entire struct base and may overlap. They may not target another overlay, may not themselves be structs, and may not partially overlay a struct base (NPL 1.5.1 §9.3). There is no blanket byte-alignment rule: the specification itself uses a 2-bit `ing_port_num[9:8]` overlay (§4.2.5).
- Check the pinned back-end's mapping directives for new top-level functions. If it reports `<fn> is not in the physical component list`, provide the required mapping or place logic in a compatible already-mapped function. Directive/mapping requirements are target compiler constraints, not universal language syntax.
- Tables and buses have hardware capacity limits (table/TCAM depth, container width). Treat every capacity number as a chip fact from project/iPipe evidence, never a memorized constant; exceeding it is a `resource overflow`, not a syntax bug.
- No duplicate definitions and no duplicate field name in the same bus/scope. Establish header/field validity before use through the guard or validity mechanism supported by the pinned dialect and current source.
- See `npl-idioms.md` for generation patterns, `npl-compile-diagnostics.md` for stage-gated diagnosis, and `docs-index.md` for the smallest normative reference needed.

## Review and Test Checklist

- Review parser/table/function/header/bus declarations, stage placement, widths/offsets, validity propagation, control-flow and unmatched/error/drop/trap/mirror behavior.
- Check all producers and consumers of changed fields, table keys/results, logical buses, and special functions; compare current source precedents and cite the revision.
- Generate deterministic packet/input fixtures for valid, nearest width/length boundary, malformed/truncated, missing/invalid field, duplicate/reordered, compatibility, and expected drop/trap cases.
- Assert only the external packet/API/telemetry boundary. Record test IDs, expected bytes/fields/status, fixture hash, and independent expected-result source.
