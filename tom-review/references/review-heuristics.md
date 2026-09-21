# Review Heuristics

Language-agnostic reading methodology for the two axes. Apply by reading the FIXED-baseline diff and its context; never compile or run. Each heuristic is tagged with the axis it feeds. When a heuristic fires, emit a finding with the axis, path/symbol/line, and evidence that reproduces the reasoning on paper.

## Reading Passes

Run these passes over every changed symbol.

- **Data-flow tracing** [Standards + Spec]: follow each external input end to end — where it enters, what transforms it, where it is used. On the path look for missing validation (no null/range check), type mismatch (a string used as an int), and missing escaping (raw input concatenated into a sink). Missing convention-level checks feed Standards; wrong resulting behavior feeds Spec.
- **Boundary derivation** [Spec]: construct extreme inputs — null/empty, zero, max value/overlong, concurrency, timeout, disconnect — and check the code stays correct against the required behavior.
- **Adversarial evaluation** [Spec + Standards]: read the key logic as a malicious user (can crafted input bypass a check or trigger an exception?), a careless caller (is the API easy to misuse; are wrong arguments guarded?), and an operator (does missing/wrong config fail fast; is the error message diagnosable?).
- **Variant analysis** [same axis as the seed finding]: after finding one defect, search the Change Set for the same shape elsewhere and flag the variants.
- **Recovery-path review** [Spec]: for a method with multiple external side effects (DB / file / RPC / message / charge), check that a later-step failure undoes or compensates the earlier step, that retries have backoff and a max count, and that a caught exception propagates a failure signal rather than only logging.
- **Consistency review** [Spec]: for a write, check the other copies of the same data (cache / search index / redundant table / in-memory state) are updated or invalidated in the same logic, multi-table updates share one transaction, and a read-after-write cannot see a stale replica/cache value.
- **Contract compatibility** [Standards]: for a signature/return-semantics change or a public interface delete/rename, trace all callers and confirm each is adapted; check wire/message-format changes stay forward-compatible.
- **Self-refutation** [both]: before emitting any finding, try to disprove it — is there context that makes the code safe, or a framework-level guard already in place? Emit only when it cannot be refuted. See [`false-positive-suppression.md`](false-positive-suppression.md).

## Semantic Defect Catalog (D-01..D-07)

Dimensions that require understanding the semantic path — pattern-matching cannot judge them. Each defect: trigger / exclusion / axis. Only raise findings at MEDIUM or above (see [`severity-taxonomy.md`](severity-taxonomy.md)).

### D-01 Multi-step rollback / partial-completion recovery [Spec]

- **Trigger**: one method has >=2 write operations with external side effects (DB / file / RPC / message / charge); a later step failing (throw or error return) leaves an earlier side effect undone, and no transaction wraps the whole operation.
- **Exclusion**: wrapped by a declared/explicit transaction; a compensation / reconciliation / retry / eventual-consistency design exists; failure is logged for deferred handling and no strong-consistency requirement in Spec.
- **Boundary**: the transaction exemption applies only when the body is all local DB writes. A remote call (HTTP / RPC / MQ send) inside the body is not covered — a rollback cannot undo an already-sent remote effect, and it also holds connections/locks across the network; that is a separate performance concern, not a reason to suppress the consistency finding.

### D-02 State-machine transition integrity [Spec]

- **Trigger**: after a conditional test on an entity's status/state, the new value: (a) skips a required intermediate state, (b) a `switch(status)` omits a legal enum and has no default, (c) a failure branch does not roll status back to the prior legal state, or (d) a concurrent "check-state then set-state" has a window.
- **Exclusion**: the field is not state-machine semantics; the enum is exhausted and compiler-guaranteed; an intentional, documented transition.

### D-03 Data-consistency / replica synchronization [Spec]

- **Trigger**: a write updates one copy of data (a DB row) while context shows the data also lives in a cache / search index / redundant table / in-memory state, and the same logic does not synchronize or invalidate it.
- **Exclusion**: framework auto-invalidation; eventual-consistency design; the replica is a self-healing transient (downgrade severity).

### D-04 Interface contract / caller compatibility [Standards — affected callers]

- **Trigger**: the diff changes a function/method signature (param add/remove/reorder/type, return semantics) or deletes/renames a public interface without adapting every call site.
- **Exclusion**: private method with all callers inside this diff; existing version negotiation / forward-compat; callers adapted in the same diff.
- **Output**: attach a `file:line` list of call sites and judge each as adapted or not.

### D-05 Comment / code semantic contradiction [Standards; escalate to Spec if it hides a real defect]

- **Trigger**: an inline/method comment describes behavior that contradicts the adjacent code.
- **Exclusion**: comment is a stale TODO not affecting correctness (drop or LOW); comment is an example, not a contract.
- **Output**: evidence must give (1) the intent the comment states, (2) the actual code behavior, (3) the specific contradiction.

### D-06 Field mis-assignment / narrowing precision loss [Spec]

- **Trigger**: (a) assignment/setter sides have clearly mismatched field semantics (a value meaning A written to field B), or a property-copy mis-maps fields; (b) a narrowing cast (long→int, double→float, int64→int32) with no range check; (c) floating-point compared with `==`/`!=`.
- **Exclusion**: intentional cast with a known-safe range; a tolerance comparison already exists.

### D-07 Common defects — div-zero / uninitialized / switch-default / infinite loop [Spec]

- **Trigger**: (a) a divisor/modulus operand is a variable or call result with no `!= 0` check before use; (b) a variable initialized only in some branches but used after the join; (c) a non-empty `switch/case` branch with no `break`/`return`/explicit fallthrough; (d) an unconditional loop or recursion with no exit.
- **Exclusion**: divisor is a known non-zero constant; language guarantees zero-value init; intentional, annotated fallthrough.

## Baseline severity fallback

When a dimension file marks a severity explicitly, honor that mark. Only when unmarked, fall back to the taxonomy in [`severity-taxonomy.md`](severity-taxonomy.md). Do not let a generic fallback silently pull an explicitly-conditional-HIGH item back down. Style-only issues (naming suggestions, formatting) are not emitted as findings.
