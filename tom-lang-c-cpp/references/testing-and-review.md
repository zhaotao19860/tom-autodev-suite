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
- Tie each blocking issue to a path/symbol, revision, acceptance criterion, and reproducible behavior.

## Failure Classification

Classify remote evidence as `CODE_FAILURE`, `TEST_FAILURE`, `ENV_UNSATISFIED`, `ENV_TRANSIENT`, `PIPELINE_TRANSIENT`, or `REVISION_MISMATCH`. Only a confirmed code/test cause can produce a repair plan; environment and revision failures never justify a business patch. Route all confirmed blockers through `tom-diagnose`.

## iPipe-Only Execution

The Mac may inspect C/C++ and generate source or product-test patches. Compilation, sanitizers, unit tests, regression, integration, packaging, and release run only in the pinned iPipe environment. Preserve the command, toolchain/image digest, revisions, fixture hash, stage/job IDs, and log/evidence references in the remote result.
