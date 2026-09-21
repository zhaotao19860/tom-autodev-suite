# NPL Compile Diagnostics

Fix/triage detail for NPL compile failures. Complements `error-patterns.md`, which is the routing taxonomy (which canonical class, who owns it); this file is *how to read the failure and where the fix goes*. Language-level only: the Mac control plane never invokes the compiler, simulator, or NCS — read these signals from iPipe evidence and reason about the source, then return the change through `tom-plan` / `tom-implement` / `tom-review`.

Full worked examples: `npl-docs/NPL_Compilation_Error_Fixup_Examples_Baidu.md`.

## Two-stage compile model — classify the stage FIRST

NPL compilation has front-end and back-end stages, followed by SDKLT/LTT integration and (when requested) simulator or product tests. The fix differs by stage. Identify the emitting job and freeze its revision/profile before touching the source.

- **nlc front-end** — syntax / type / sizing / validity / undefined-identifier checks. Diagnostics may include `ERROR:[TACx-xx-xxxx]` or `(N)Errors`. Check the actual code and construct before choosing a source repair.
- **xfc back-end** — placement, resource analysis and flex-editor / bus / MPB / FSL generation. Diagnostics may include `CRITICAL: xfc.*`, `CRITICAL: main`, `CRITICAL: fsl`, or a failed subordinate command. A passed nlc stage does not validate these stages. `KeyError` alone does not prove a source constraint: it can also be a compiler defect or incompatible generated input. Establish the failing component and current source relationship first.
- **SDKLT/LTT integration** — a compile may pass while generated table/action metadata is rejected. Treat an unconsumed action, schema mismatch, or generated-artifact error as an integration failure until the current NPL mapping and generated metadata prove a source cause.
- **NPLSIM/product test** — a packet, API, drop, mirror, or telemetry mismatch is a behavior/test result, not a compile diagnosis.

### Evidence gates for resource and historical patterns

| Observation | Apply the repair only when | Otherwise |
|---|---|---|
| table/container/placement limit | the emitting stage names the resource and business/test revisions, chip target, toolchain, and environment fingerprint match the task | classify as insufficient evidence or environment capacity; do not patch from a remembered limit |
| `add_header` with `un-reachable code` | current xfc evidence points to an unconditional edit or missing data-dependent tap point | inspect the current editor path; this is a conditional pattern, not a language law |
| `Concat not supported` / 4-bit packing | the same back-end diagnostic occurs under the current profile | retain as historical compiler behavior only |
| unconsumed action fields | SDKLT/LTT evidence identifies the action field and current generated mapping | do not infer from an NPL-only success line |

### False-success trap (must guard against)

Historical wrappers continued after a back-end crash and later printed `success` or produced an RPC binary. Read the remote stage exit/result and complete stage logs, including stdout/stderr, rather than matching only the literal `CRITICAL : xfc`. A `(0)Errors` line proves at most the front-end result. Report failure when a required stage failed; if its result/log is absent or truncated, report insufficient evidence, not success. Verify generated artifacts belong to the same build/revisions before consuming them.

## Source checks selected by diagnostic

1. **Header validity** — establish validity on each use path with the actual mechanism supported by the pinned dialect; do not invent an `isValid()` API from an example.
2. **Sizing mismatch** — validate key/interface widths and assignment direction. The language permits zero extension into a wider target; a narrower target requires a legal explicit slice/conversion. See the local specification's Assignment operators row.
3. **Undefined identifier** — check spelling; ensure the file is `#include`d in the pipeline entry (`main_process.npl`) so the symbol is in scope.
4. **Stage-specific capacity diagnostic** — classify as a front-end source issue only when the nlc diagnostic identifies it; xfc placement/container errors belong to the back-end path above.
5. **Duplicate definition** — the same name defined twice (fields, functions, structs).
6. **Parser/MPB offset mismatch** — follow the actual extraction, header length and transport mapping; diagnose from parser/MPB or packet evidence rather than labelling every offset symptom an nlc error.

## Back-end (xfc) structural fixes — common shapes

Distilled from versioned fixup examples and project observations; use as hypotheses under the evidence gates above. Preserve packet semantics while testing a structural alternative.

- **`add_header` → may need a tap point.** When current xfc evidence ties `un-reachable code` to an unconditional edit, hang the `add_header` under an `if` keyed on a real field so the editor can solve reachability. Verify the tap point in the current source; this is not an unconditional language rule.
- **Unexpected added-header position.** Check parsed-header identity, header groups and intended output order. A historical case involved adding an already-parsed header; do not remove parsing or delete/re-add fields without validating the intended output bytes.
- **Trailing bytes after deletion.** A historical compiler case omitted trailing bytes for several sibling deletes; inspect contiguity and consider parent-granularity deletion when it preserves all intended fields. Validate against current output bytes.
- **Bus mapping / tap-point failures** (`Bus Mapping of component Bus failed`, `KeyError`) — a table keying directly off a parser field can fail placement; route the value through the object/command bus in a function first, then key the table off the bus field.
- **Container / placement exhaustion** (`Bus placement failed`, `Not enough HFE commands`, `Insufficient container width`, `16b mux exceeded`) — reduce extracted width (extract only the needed bits), pack fields into a wider declared bus field, force a new HME stage where the directive exists, or move data via MPB instead of parser extraction. These are budget problems, not syntax.
- **4-bit alignment (observed pattern)** — some compiler profiles copy in 4-bit container units. Apply this only when the pinned profile emits `Concat not supported`; do not encode it as a universal NPL rule.
- **Unconsumed action fields** — NPL may pass while SDKLT/LTT fails because a table's action fields are never consumed; either consume them (add an FSL that reads them) or remove the unused table.

## Where the fix lands

- Front-end pitfalls: fix in the offending NPL source (bus/table/struct/parser file).
- Top-level function errors (`not in the physical component list`): check the target back-end's mapping requirement; add the applicable directive or use a compatible mapped function. See `npl-core-rules.md`.
- Back-end constraint errors: structural change (placement, container budget, tap point) — not a syntax retry.

Always freeze the evidence (stage/job IDs, revisions, the exact `ERROR`/`CRITICAL` line) and route through `error-patterns.md` before proposing a repair.

Return to the caller: stage/component, canonical class (or unresolved classification), exact evidence references and identities, observed facts, one hypothesis with its confirming/disproving check, proposed source/test scope, and remaining evidence. Put these in the caller's existing artifact fields; this guide does not create a second workflow schema.

Example: nlc says `(0)Errors`, FSL logs `CRITICAL: fsl ... Concat not supported`, and the wrapper prints `success`. The result is a failed back-end stage, not compile success; a 4-bit packing change remains a hypothesis until the current compiler and output contract validate it.
