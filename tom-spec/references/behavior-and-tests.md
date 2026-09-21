# Behavioral Contract and Independent Test Design

For each acceptance point, state the triggering input/precondition, observable output or state change, error semantics and compatibility expectation. “Support IPv6” is incomplete without the interface, input family, expected response and invalid-input behavior. Keep construction choices separate from required outcomes.

Use a decision/state table when behavior depends on combinations or history: existing/new entity, allowed/denied request, duplicate/retry, success/timeout, absent/malformed field. Cover meaningful partitions and nearest boundaries rather than every cross-product. For stateful actions include duplicate delivery, interruption after a side effect, partial completion, cancellation and recovery where the exposed contract has those states.

Test the highest stable boundary that observes the promise: packet/API/CLI/output/status. Independent product tests belong with the same behavioral slice. Focused unit tests can supplement localization; internal mocks or generated implementation output cannot be the only oracle for the external acceptance criterion.

Expected results come from approved requirements, protocol specifications or a separately justified reference. Do not derive the oracle by calling the changed function and recording its output. Pin fixture origin, request bytes/fields, expected response/error, setup/cleanup owner and version. Nondeterministic fields need explicit invariants, tolerances or canonicalization; performance assertions need workload, environment and threshold definitions.

For every behavior record the existing test, new/changed test, remote stage, or an explicit human-verification reason. A missing ability in the configured runner is an environment decision, not permission for a local fallback. Planned tests stay planned; only matching iPipe receipts establish execution.

For a merged `{spec, dag}` output, derive both halves from the same behavior IDs and acceptance union. Validate full coverage, unique IDs and genuine dependencies together before returning the bundle. A prose claim of complete coverage cannot replace the schema's traceability/coverage fields.
