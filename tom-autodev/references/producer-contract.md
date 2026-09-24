# Producer Contract

Before locking a ProducerJob as FULFILLED, the worker uses the same approval-free
`PhaseProtocol.validate_draft` as completion: schema, task, predecessor, content/hash,
revision and evidence checks. Merged SPEC/TASKS uses the same content validator for both
halves. A rejection leaves the job retryable when the content itself is invalid.

For a historical FULFILLED invalid draft, normal resume/submit revalidates it. Only proven
content invalidity for the same current action permits a transactional CAS reopen; the old
draft, payload, hashes, validation and matching cache entries are archived in
`producer_job_attempts`. Model receipts and approval history remain. Valid drafts awaiting
approval, stale actions, damaged context, published artifacts and uncertain external writes
are not automatically replaced. A corrected draft receives its own approval input hash.

The suite's worker owns execution order. A child skill is a bounded producer of content, not a second workflow engine. These rules apply to GRILL, SPEC, TASKS, PLAN, IMPLEMENT, REVIEW and DIAGNOSE.

## Read once, return one draft

1. Consume the current ProducerJob/action and its immutable input artifacts. Copy task IDs, input hashes, revisions, module identity and failure identity from that context; do not reconstruct them from chat history or a later checkout.
2. Read the action's `result_schema` in `../schemas/` and the applicable project/language references. JSON Schema checks shape; context checks also bind acceptance coverage, predecessors, revisions and ownership. A syntactically valid draft can still be wrong.
3. Return only the schema's content object as `DraftContent` to this job using `python3 tom-autodev/scripts/cli.py submit-draft CARD_OR_RUN JOB_ID DRAFT.json`. The JSON file contains the content object only; do not wrap it in an `ArtifactEnvelope`. A requested merged SPEC job returns exactly `{"spec": <spec content>, "dag": <task-dag content>}` in one producer turn; apply both skills' rules without starting another workflow or agent merely to fill the second half.
4. The worker constructs and hashes `ArtifactEnvelope`, validates and persists the content, and completes the phase when its gate permits. If it returns `APPROVAL_REQUIRED`, request the exact returned gate and `approval_input_hash` with the existing `request-approval` command, then stop until approval. Do not regenerate or alter the saved candidate. Producer output never invents `approval_id`, KU/iCafe receipts, artifact IDs or verification evidence. Hash fields required *inside* a content schema must come from their actual pinned inputs or the suite's canonical calculation.

The phase schema and actual action take precedence over descriptive field lists in a skill. If a required field cannot be truthfully supplied, return a concrete missing-input/contract explanation to the parent instead of adding arbitrary JSON properties, placeholders or a fabricated hash.

## Gate and recovery ownership

- Gate labels in documents describe the full flow; the current run's resolved action determines which gate applies. A merged, auto or ungated phase does not create an extra approval or model turn just because its standalone skill mentions one.
- The child does not call `advance`, `complete-phase`, iCafe/KU/iCode/iPipe adapters, or directly route a failure. It can submit its one draft with `submit-draft` and report the resulting validation error.
- Reuse a saved draft for an unchanged action. On validation failure, correct only the reported content problem through the supported revision/retry interface. If that interface refuses a correction, report the conflict; do not write the state database or generate a new run to bypass it.
- On interruption, inspect durable job/workspace state and actual files before doing work again. A checklist describes intended work; it is not proof that an edit, commit, trigger or publication occurred.
- Local source reading, source generation and suite helper validation are separate from project execution. Business compilation, simulation and tests require the configured iPipe runner; source reasoning is not a passing test receipt.

## Knowledge loading

Load the project profile named by the run and the language profiles relevant to the changed files, then only references needed by the affected constructs. Internal suite links are supported package dependencies. No phase requires installing npl-coder, code-review-qa, tom-autodebug, superpowers or a graph-search skill. A graph is optional supporting evidence: record its freshness when used; otherwise inspect symbols/callers/configuration with precise text search and cite file/revision evidence.

For archival envelope details see [phase-protocol.md](phase-protocol.md); the model's output boundary is the content-only contract above.
