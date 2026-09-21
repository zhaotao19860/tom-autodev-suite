# C/C++ Testing and Review

## Test Design

- Derive expected results from the approved Spec or an independent protocol/project fact source.
- Assert externally observable behavior at the selected stable interface.
- Cover valid input, the nearest boundary, malformed/truncated input, compatibility behavior, and one regression for the closest existing path when applicable.
- Keep fixtures deterministic and identify ownership of cleanup, resources, threads, and external services. Include empty input, maximum lengths/counts, duplicate/reordered input, timeout/cancellation, partial failure, and retry/idempotency cases when the interface exposes them.
- Test the public wire/RPC/CLI/API boundary rather than private functions. Record fixture provenance, setup/teardown, expected status/error, output invariants, and cleanup owner.
- Record test IDs, product-test repository revision, and iPipe unit/regression/integration stage parameters in the Task Plan; do not claim local execution evidence.

## Source Review

- Inspect changed symbols and all callers before judging API or ABI changes.
- Check lifetime/ownership, error propagation, integer and buffer bounds, signedness/overflow, nullability, concurrency/locking, cancellation, serialization/versioning, ABI/layout, platform assumptions, and observability.
- Flag dead code, speculative abstraction, duplicated policy, hidden global state, and changes not linked to Spec behavior.
- Tie each blocking issue to a path/symbol, revision, acceptance criterion or mandatory language/project contract, and reproducible behavior.

## Concrete Language Checks

Apply checks triggered by the changed code. These are source-reading rules, not a claim that a sanitizer or compiler ran. A pattern is reportable only with a reachable trigger and consequence; respect the pinned C/C++ standard, ownership contract, and toolchain.

| Area | Trigger to trace | Evidence that can exclude the candidate |
|---|---|---|
| Lifetime and views | Returning/storing a pointer, reference, `c_str()`, iterator, `string_view`, or `span` into a local, freed, moved, or resized object; asynchronous callback captures an object beyond its lifetime | Proven owner outlives every use and no invalidating operation occurs; a real owning copy is made |
| Container invalidation | `erase` followed by reuse/increment of the invalid iterator; `vector` growth followed by reuse of old pointers/references | Use of the iterator returned by `erase`; a capacity/lifetime guarantee proved for the full path; container-specific stability rules |
| Ownership and cleanup | Allocation/acquisition followed by early return, exception, partial initialization, or cancellation; double release; deletion through an incompatible base type | RAII releases every acquired resource; documented ownership transfer; correct destruction mechanism. A missing local `delete` alone does not prove a leak |
| Length and arithmetic | External packet/count length enters allocation, offset, copy, or indexing; `count * element_size` overflows before bounds checking; signed/unsigned conversion expands a negative size | Check on the original representation and overflow-safe arithmetic establishes both source and destination bounds before access |
| Strings and buffers | Copy/format length reaches capacity; a truncated string is later consumed as NUL-terminated | Explicit termination and valid lengths for every consumer. Replacing `strcpy` with `strncpy` alone is not a fix because termination is not guaranteed |
| Undefined behavior | Signed overflow, excessive/negative shift, uninitialized read, dangling access, misaligned/aliasing-violating cast of packet bytes | Proven operand ranges/alignment/object lifetime; a defined conversion/copy representation. `volatile` does not establish thread synchronization |
| Concurrency | Check-then-act without shared synchronization; inconsistent lock order; callback races teardown; atomic publication without the needed ordering | A demonstrated happens-before relation, consistent ownership/locking, or a documented single-threaded boundary that covers all callers |
| Wire format and ABI | Struct layout or enum/width changes cross a public boundary; native endian/padding is serialized; packed fields are read through unaligned pointers | Explicit versioned encoding/decoding, compatible layout contract, byte-order conversion, and safe field reads on supported architectures |
| Hot-path cost | New allocation, blocking I/O, unbounded loop, global lock, or copying on a packet/latency-sensitive path | Project workload/budget and bound show acceptable cost; claim a performance defect only with concrete scale or contract evidence |

For a changed binary parser, pair the normal packet with truncated header/body, zero and maximum length, length/count disagreement, invalid enum/flags, and an overflow-sized count. For lifetime/concurrency changes, specify the ownership or interleaving that the regression must exercise. Select applicable cases; do not demand every case for unrelated edits.

## Failure Classification

Classify remote evidence as `CODE_FAILURE`, `TEST_FAILURE`, `ENV_UNSATISFIED`, `ENV_TRANSIENT`, `PIPELINE_TRANSIENT`, or `REVISION_MISMATCH`. Only a confirmed code/test cause can produce a repair plan; environment and revision failures never justify a business patch. Route all confirmed blockers through `tom-diagnose`.

## iPipe-Only Execution

The Mac may inspect C/C++ and generate source or product-test patches. Compilation, sanitizers, unit tests, regression, integration, packaging, and release run only in the pinned iPipe environment. Preserve the command, toolchain/image digest, revisions, fixture hash, stage/job IDs, and log/evidence references in the remote result.
