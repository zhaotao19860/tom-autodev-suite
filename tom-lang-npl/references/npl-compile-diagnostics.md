# NPL Compile Diagnostics

Fix/triage detail for NPL compile failures. Complements `error-patterns.md`, which is the routing taxonomy (which canonical class, who owns it); this file is *how to read the failure and where the fix goes*. Language-level only: the Mac control plane never invokes the compiler, simulator, or NCS — read these signals from iPipe evidence and reason about the source, then return the change through `tom-plan` / `tom-implement` / `tom-review`.

Full worked examples: `npl-docs/NPL_Compilation_Error_Fixup_Examples_Baidu.md`.

## Two-stage compile model — classify the stage FIRST

NPL compilation is two stages, and the fix differs completely by stage. Identify the stage before touching the source.

- **nlc front-end** — syntax / type / bit-width / `isValid()` / undefined-identifier checks. Emits `ERROR:[TACx-xx-xxxx]` or `(N)Errors`. This is a *language-layer* problem: go through the 6 pitfalls below.
- **xfc back-end** — constraint analysis + flex-editor / bus / MPB / FSL code generation. Emits `CRITICAL : xfc.*` (e.g. `un-reachable code`, ITILE `More number of rules`, `16b mux exceeded`, MPB `KeyError`, `should not depend on output from <stage>`). The source is often perfectly legal syntax (nlc already passed, may even show `(0)Errors`) — the failure is a *hardware constraint / resource / reachability* violation. Do not keep retrying syntax fixes; the change here is structural (placement, container budget, tap points).

### False-success trap (must guard against)

After an xfc `CRITICAL` crash, later RPC stages can still emit a binary and the build wrapper can still print `success`. Confirm the compile actually passed by scanning stdout for `CRITICAL : xfc`. A `(0)Errors` line in `nscp.log` only proves the nlc front-end passed — it does NOT prove xfc succeeded. Treat a build that shows `success` while stdout contains `CRITICAL : xfc` as a failure.

## The 6 canonical front-end (nlc) pitfalls — check these first

1. **`isValid()` missing** — header fields used in table keys must be guarded with an `isValid()` check.
2. **Bit-width mismatch** — field widths must match exactly between bus/struct definitions and table keys or assignments.
3. **Undefined identifier** — check spelling; ensure the file is `#include`d in the pipeline entry (`main_process.npl`) so the symbol is in scope.
4. **Resource overflow** — TCAM/bus/container too full; the limit is a chip fact (confirm against project/chip rules, not memory).
5. **Duplicate definition** — the same name defined twice (fields, functions, structs).
6. **Parser/MPB offset mismatch** — byte offsets must align between parser extraction and bus fields; carry width markers (e.g. `parser_vhlen`) through parser→EP consistently.

## Back-end (xfc) structural fixes — common shapes

Distilled from the fixup examples; use as a fix direction, not a copy-paste recipe.

- **`add_header` → needs a tap point.** The classic root cause of xfc `un-reachable code` is an unconditional `add_header`: the editor cannot solve a reachability equation for it. Fix by hanging the `add_header` under an `if` branch keyed on a real field (e.g. an encap index), so the editor has a condition. This is unrelated to `&&` gating style.
- **Do not re-add an already-parsed header.** If the packet was parsed into a header, adding it again without first deleting it corrupts position. Prefer not parsing a header you intend to add; delete-then-add can also strip fields unexpectedly.
- **Merge sibling deletes.** Deleting many contiguous sub-headers individually can under-delete (miss trailing bytes); delete at the parent header granularity instead.
- **Bus mapping / tap-point failures** (`Bus Mapping of component Bus failed`, `KeyError`) — a table keying directly off a parser field can fail placement; route the value through the object/command bus in a function first, then key the table off the bus field.
- **Container / placement exhaustion** (`Bus placement failed`, `Not enough HFE commands`, `Insufficient container width`, `16b mux exceeded`) — reduce extracted width (extract only the needed bits), pack fields into a wider declared bus field, force a new HME stage where the directive exists, or move data via MPB instead of parser extraction. These are budget problems, not syntax.
- **4-bit alignment** — copies happen in 4-bit container units; sub-field packing must align on 4-bit boundaries or the back end reports `Concat not supported`.
- **Unconsumed action fields** — NPL may pass while SDKLT/LTT fails because a table's action fields are never consumed; either consume them (add an FSL that reads them) or remove the unused table.

## Where the fix lands

- Front-end pitfalls: fix in the offending NPL source (bus/table/struct/parser file).
- Top-level function errors (`not in the physical component list`): the new egress/ingress top-level function needs an `@NPL_PRAGMA(...mapping...)` entry, or merge the logic into an already-mapped function rather than adding a new top-level one. See `npl-core-rules.md`.
- Back-end constraint errors: structural change (placement, container budget, tap point) — not a syntax retry.

Always freeze the evidence (stage/job IDs, revisions, the exact `ERROR`/`CRITICAL` line) and route through `error-patterns.md` before proposing a repair.
