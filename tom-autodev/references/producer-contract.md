# Producer Contract

The suite's worker owns execution order. A child skill is a bounded producer of content, not a second workflow engine. These rules apply to GRILL, SPEC, TASKS, PLAN, IMPLEMENT, REVIEW and DIAGNOSE.

## Read once, return one draft

1. Consume the current ProducerJob/action and its immutable input artifacts. Copy task IDs, input hashes, revisions, module identity and failure identity from that context; do not reconstruct them from chat history or a later checkout.
2. Read the action's `result_schema` in `../schemas/` and the applicable project/language references. JSON Schema checks shape; context checks also bind acceptance coverage, predecessors, revisions and ownership. A syntactically valid draft can still be wrong.
3. Return only the schema's content object as `DraftContent` to this job through the provided `submit-draft` interface. A requested merged SPEC job returns exactly `{"spec": <spec content>, "dag": <task-dag content>}` in one producer turn; apply both skills' rules without starting another workflow or agent merely to fill the second half.
4. The worker constructs and hashes `ArtifactEnvelope`, requests the applicable gate, publishes according to the phase policy, persists receipts and completes the phase. Producer output never invents `approval_id`, KU/iCafe receipts, artifact IDs or verification evidence. Hash fields required *inside* a content schema must come from their actual pinned inputs or the suite's canonical calculation.

The phase schema and actual action take precedence over descriptive field lists in a skill. If a required field cannot be truthfully supplied, return a concrete missing-input/contract explanation to the parent instead of adding arbitrary JSON properties, placeholders or a fabricated hash.

## Gate and recovery ownership

- Gate labels in documents describe the full flow; the current run's resolved action determines which gate applies. A merged, auto or ungated phase does not create an extra approval or model turn just because its standalone skill mentions one.
- The child does not call `advance`, `complete-phase`, iCafe/KU/iCode/iPipe adapters, or directly route a failure. It can submit its one draft and report the resulting validation error.
- Reuse a saved draft for an unchanged action. On validation failure, correct only the reported content problem through the supported revision/retry interface. If that interface refuses a correction, report the conflict; do not write the state database or generate a new run to bypass it.
- On interruption, inspect durable job/workspace state and actual files before doing work again. A checklist describes intended work; it is not proof that an edit, commit, trigger or publication occurred.
- Local source reading, source generation and suite helper validation are separate from project execution. Business compilation, simulation and tests require the configured iPipe runner; source reasoning is not a passing test receipt.

## Knowledge loading

Load the project profile named by the run and the language profiles relevant to the changed files, then only references needed by the affected constructs. Internal suite links are supported package dependencies. No phase requires installing npl-coder, code-review-qa, tom-autodebug, superpowers or a graph-search skill. A graph is optional supporting evidence: record its freshness when used; otherwise inspect symbols/callers/configuration with precise text search and cite file/revision evidence.

For archival envelope details see [phase-protocol.md](phase-protocol.md); the model's output boundary is the content-only contract above.
