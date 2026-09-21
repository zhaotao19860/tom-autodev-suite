# NPL Generation Idioms

Language-level construction patterns for generating and reviewing NPL. These are shape rules, not project facts: confirm concrete file names, bus names, table budgets, and chip macros against the target project (`tom-project-xflow`) and current source before applying. Project-specific names below are illustrative examples, not the contract.

## 1. Feature-Gating Wrapper (real + stub)

Gate every new feature behind a `#define` and give it a matched empty stub so the pipeline still links when the feature is off.

```npl
// feature_knobs.npl
#define MY_FEATURE 1

// <feature>/<feature>_process.npl  — real implementation
function my_feature_do_something() { ... }

// wrapper/<feature>_process.npl    — stub, guarded by #ifndef
#ifndef MY_FEATURE
function my_feature_do_something() { /* empty stub */ }
#endif
```

Rules:
- The stub signature must match the real function exactly; a mismatch changes the compiled component list.
- A new feature is a set of files (parser / process / tables / flex-editor) plus one wrapper stub, not a single edit.
- Never leave a top-level function referenced but undefined in the disabled path.

## 2. Strength-Based Arbitration

When more than one table or stage can write the same result field, resolve by strength, not by write order. Higher strength wins; an ACL/override layer sits at the top of the chain.

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

The flex editor runs its zones in REVERSE order (highest zone first). Header edits must be placed in the zone that matches the layer being rewritten, because a later-running (lower) zone sees the output of the earlier (higher) one.

| Zone | Typical purpose (project example) |
|------|-----------------------------------|
| 4 | L3/L4 rewrite (TTL, TOS, checksum) |
| 3 | L2 header ops (MACDA/MACSA/VLAN) |
| 2 | L3/L4 tunnel encap |
| 1 | L2 tunnel encap |
| 0 | System headers (CPU, mirror, ERSPAN) |

Rules:
- Execution is 4→0; reason about edits in that order, not top-to-bottom in the file.
- `add_header` / `delete_header` placement is order-sensitive — see `npl-compile-diagnostics.md` for the `add_header` tap-point rule and the delete-merge pitfall.

## 5. Bus-Architecture Model

Metadata flows between stages through named buses, not through shared globals. Each field has exactly one producing stage per pass and one or more downstream consumers.

Typical bus roles (names are project-specific — confirm in source):
- an object/result bus — lookup results (destination, next-hop, forwarding index, etc.)
- a command bus — per-packet commands (drop, copy-to-CPU, edit control, etc.)
- separate ingress vs egress buses, plus scratch buses for intermediate values.

Rules:
- A field must be assigned before any stage reads it; an unassigned bus field is a compile error, not a silent zero.
- Do not write the same bus field from two producers unless the arbitration/strength model allows it (multiple parser extractions into one container is rejected).
- Field width in the bus definition must match every producer and consumer assignment exactly.

## 6. NPL Critical Constraints (check FIRST when generating or reviewing)

These are the language/hardware invariants that most often break a generated change. The authoritative rows also live in `npl-core-rules.md`; this table is the generation-time quick check.

| Constraint | Rule | Symptom when violated |
|-----------|------|-----------------------|
| `isValid()` guard | Header fields used in table keys / conditions must be guarded by `isValid()` | "field used without validity check" |
| Bit-width match | Every assignment must match the exact declared bit width | "type mismatch" / width error |
| Table / TCAM depth | Tables have hardware capacity limits (a chip fact) | "resource overflow" |
| Parser / MPB offsets | Byte offsets must stay consistent parser↔bus↔MPB | "offset mismatch" |
| Field uniqueness | No duplicate field name in the same bus/scope | "duplicate definition" |
| Overlay alignment | Struct overlays must be byte-aligned | "alignment error" |
| No `return` | NPL has no `return`; wrap early-exit logic in `if (...)` | parse/semantic error |
| Top-level mapping | Egress/ingress top-level functions need an `@NPL_PRAGMA(...mapping...)` entry | "not in the physical component list" |

For the two-stage compile model and the concrete fix procedure behind these symptoms, see `npl-compile-diagnostics.md`.
