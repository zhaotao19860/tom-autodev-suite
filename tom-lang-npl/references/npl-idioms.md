# NPL Generation Idioms

Construction patterns for generating and reviewing NPL. Confirm file names, buses, table budgets, chip macros, and compiler behavior against the caller's project profile and current source. The wrapper layout and numbered editor zones below are project examples, not universal NPL requirements. Normative language rules live in `npl-core-rules.md`.

## 1. Feature-Gating Wrapper (real + stub)

When the project uses compile-time feature gates, follow its real/stub include convention so the disabled path remains defined. A gate must select exactly one implementation; merely defining a macro does not exclude the real file.

```npl
// feature_knobs.npl
#define MY_FEATURE 1

// <feature>/<feature>_process.npl  — real implementation
#ifdef MY_FEATURE
function my_feature_do_something() { ... }
#endif

// wrapper/<feature>_process.npl    — stub, guarded by #ifndef
#ifndef MY_FEATURE
function my_feature_do_something() { /* empty stub */ }
#endif
```

Rules:
- The stub signature must match the real function exactly; a mismatch changes the compiled component list.
- Change only the parser/process/table/editor files the feature actually needs; do not create empty layers to match the example.
- Never leave a top-level function referenced but undefined in the disabled path.

## 2. Strength-Based Arbitration

When the project resolves competing table results by strength, extend its declared strength chain and tie behavior. Establish numeric priority from the current declaration; do not infer a universal ACL order from the example below.

```
result field = highest-strength writer among the candidate producers
  (e.g. a destination field: UC lookup < default route < ACL override)
```

Rules:
- Do not rely on later assignments silently overwriting earlier ones; make the priority explicit through the strength mechanism the project uses.
- When adding a new producer of an existing field, place it correctly in the strength chain and re-check every other producer/consumer of that field.

## 3. Chip-Conditional Compilation

Isolate chip-specific behavior behind chip macros; keep the common path unconditional.

```npl
#ifdef TRIDENT5_X12
  // chip-X-specific code
#elif defined(TRIDENT4_X9)
  // chip-Y-specific code
#endif
```

Rules:
- Chip overrides live in the chip's own subdirectory (e.g. `trident5_x12/`), not inline in the shared file, unless the project already does otherwise.
- Do not assume a macro name — read `feature_knobs.npl` / chip directive files for the exact symbol.
- Chip capacity numbers (table depth, container widths) are chip facts: never hard-code them from memory; treat them as iPipe/project evidence.

## 4. Zone-Based Editing (Flex Editor)

In the XFlow reference layout, numbered editor zones run highest first. Confirm the actual ordering and header dependencies from the current program/editor configuration before choosing a zone.

| Zone | Typical purpose (project example) |
|------|-----------------------------------|
| 4 | L3/L4 rewrite (TTL, TOS, checksum) |
| 3 | L2 header ops (MACDA/MACSA/VLAN) |
| 2 | L3/L4 tunnel encap |
| 1 | L2 tunnel encap |
| 0 | System headers (CPU, mirror, ERSPAN) |

Rules:
- For the illustrated 4→0 layout, reason about edits in that order. Use the target's configured order when it differs.
- `add_header` / `delete_header` placement is order-sensitive — see `npl-compile-diagnostics.md` for conditional tap-point and delete-granularity cases.

## 5. Bus-Architecture Model

Metadata flows between stages through named buses. Record initialization, all writers, ordering/arbitration, and downstream readers for each changed field; several writers may be intentional in a legal strength or sequential path.

Typical bus roles (names are project-specific — confirm in source):
- an object/result bus — lookup results (destination, next-hop, forwarding index, etc.)
- a command bus — per-packet commands (drop, copy-to-CPU, edit control, etc.)
- separate ingress vs egress buses, plus scratch buses for intermediate values.

Rules:
- Establish the value or specified default on every path before a field is consumed; do not assume the compiler diagnoses every uninitialized use.
- Do not write the same bus field from two producers unless the arbitration/strength model allows it (multiple parser extractions into one container is rejected).
- Check declared width across producers and consumers, including intentional zero extension, slicing, and any target-specific packing constraint.

## 6. NPL Critical Constraints (check FIRST when generating or reviewing)

Use the checklist below to select a check, then apply the language rules in `npl-core-rules.md` or the current target's evidence. A historical diagnostic is not a language invariant.

| Constraint | Rule | Symptom when violated |
|-----------|------|-----------------------|
| Header validity | Establish validity before key/condition use with the pinned dialect's actual mechanism | invalid-header behavior or compiler diagnostic |
| Assignment sizing | Wider targets zero-extend; narrower targets require an explicit legal conversion/slice | width error or unintended value |
| Table / TCAM depth | Tables have hardware capacity limits (a chip fact) | "resource overflow" |
| Parser / MPB offsets | Byte offsets must stay consistent parser↔bus↔MPB | "offset mismatch" |
| Field uniqueness | No duplicate field name in the same bus/scope | "duplicate definition" |
| Overlay legality | Check base/type restrictions; sub-byte overlays on bit fields are allowed by the language | invalid overlay declaration or target packing constraint |
| No `return` | NPL has no `return`; wrap early-exit logic in `if (...)` | parse/semantic error |
| Top-level mapping | Check the target compiler's directive requirements | "not in the physical component list" |

For the two-stage compile model and the concrete fix procedure behind these symptoms, see `npl-compile-diagnostics.md`.
