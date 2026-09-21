# NPL Core Rules

- Confirm construct kind and its legal fields before editing: parser nodes, logical tables, functions, headers, logical bus fields, and special functions have different contracts.
- Match existing project syntax and field naming exactly; use current source precedents before official examples.
- Keep bit widths, offsets, field validity, stage placement, bus direction, and control-flow transitions explicit.
- Preserve unmatched, malformed, drop, trap, mirror, and error behavior unless the approved Spec changes it.
- Do not assume a C/C++ analogy defines NPL semantics. Verify behavior in current NPL sources or approved documentation.
- Test through an external product boundary and trace each assertion to an approved behavior; do not assert generated internal representation unless the Spec makes it observable.

## Hard Language Constraints

- NPL has no `return` statement. Wrap early-exit / conditional logic in `if (condition) { ... }` instead of returning.
- Egress and ingress top-level functions (each function called from the pipeline entry) must have an `@NPL_PRAGMA(<fn>, mapping:"<physical block>")` entry in the chip directive file, or the compile reports `<fn> is not in the physical component list`. To add pipeline logic, merge it into an already-mapped function rather than declaring a new top-level function.
- Tables and buses have hardware capacity limits (table/TCAM depth, container width). Treat every capacity number as a chip fact from project/iPipe evidence, never a memorized constant; exceeding it is a `resource overflow`, not a syntax bug.
- No duplicate definitions and no duplicate field name in the same bus/scope; struct overlays must be byte-aligned.
- See `npl-idioms.md` for the full critical-constraints table and generation patterns, and `npl-compile-diagnostics.md` for the front-end vs back-end fix procedure.

## Review and Test Checklist

- Review parser/table/function/header/bus declarations, stage placement, widths/offsets, validity propagation, control-flow and unmatched/error/drop/trap/mirror behavior.
- Check all producers and consumers of changed fields, table keys/results, logical buses, and special functions; compare current source precedents and cite the revision.
- Generate deterministic packet/input fixtures for valid, nearest width/length boundary, malformed/truncated, missing/invalid field, duplicate/reordered, compatibility, and expected drop/trap cases.
- Assert only the external packet/API/telemetry boundary. Record test IDs, expected bytes/fields/status, fixture hash, and independent expected-result source.
