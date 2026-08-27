# Task 4: Real iCafe, KU and Knowledge Synchronization

## Files

- Create `scripts/cli_transport.py`, `scripts/clients/ku_client.py`, and `scripts/knowledge_sync.py`.
- Modify `scripts/clients/icafe_client.py` and `scripts/orchestrator.py` only for safe adapter wiring needed by this task.
- Add/update `scripts/tests/test_adapters.py` and `scripts/tests/test_knowledge_sync.py`.

## Interfaces

- `CafeClient.snapshot(card_id: str) -> dict`
- `CafeClient.comment(card_id: str, content: str, idempotency_key: str) -> dict`
- `CafeClient.update_status(card_id: str, status: str, expected_current: str) -> dict`
- `CafeClient.close(card_id: str, status: str, reason: str, knowledge_url: str, approval: dict) -> dict`
- `KuClient.create_artifact(parent_doc_id: str, title: str, markdown: str) -> dict`
- `KuClient.update_index(doc_id: str, entry: dict) -> dict`
- `KnowledgeSync.publish_phase(run_id: str, artifact: dict) -> dict`

## Required iCafe contract

1. Use the real `icafe-cli` command shape from `/Users/tom/.comate/skills/.system/icafe`: first-session version/login preflight, `card get --space ... --sequence ... --brief`, `card smart-find` only when no explicit card is available, `comment get/create`, `card current-status`, `card next-statuses`, and `card update`.
2. Parse an explicit card as `<space>-<numeric-sequence>` using the final hyphen. Never guess a space or create a requirement card in this delivery adapter.
3. Build an immutable RequirementSnapshot containing title, HTML/body, acceptance/fields, attachments/links, status/type, responsible people, created/modified metadata, canonical card ID and content hash.
4. Check both process exit code and command-specific business status: card get/update/query require `code=200`; other iCafe commands require `status=200`. Map auth, invalid input, permission, missing object, timeout, invalid JSON and business failures to stable reason codes without exposing secret/content arguments.
5. Comments include a deterministic invisible/plain marker derived from the idempotency key. Query before create and after an unknown result; duplicate calls return the existing comment receipt rather than posting twice.
6. Status update must verify `expected_current`, require the target in `next-statuses`, update without `--no-check-status`, and re-query the card/current status before returning success.
7. Delete semantics are close/cancel only. Require a ledger-backed approved input hash, reachable configured terminal status, reason and KU URL; otherwise return `ICAFE_DELETE_UNSUPPORTED`/approval/status errors. Never call physical delete or overwrite the iCafe requirement正文.

## Required KU contract

1. Use the real `ku` CLI from `/Users/tom/.comate/skills/.system/ku-doc-manage/bin/ku` with `BAIDU_CC_USERNAME`; call `query-content`, `query-version`, `create-doc`, `edit-content`, and `publish-doc` with argument arrays.
2. KU success requires exit 0 plus `returnCode=200` (and non-false success when present). After every create/edit/publish, query the remote content/version and verify doc ID, repo ID, URL/version, and canonical content hash before returning a receipt.
3. Phase child documents are immutable. A deterministic idempotency marker/query prevents duplicate child creation; an existing matching child returns its receipt, while content/hash mismatch returns a conflict.
4. The run root document maintains only a phase index. Read current Markdown first; add one deterministic entry with `edit-content` using an exact MDSL/append operation; call `publish-doc`; then query again and verify the entry and version. Never overwrite historical child documents.
5. Use the project profile KU target. BGW is repo `sX0BTOBWJX`, parent `I15ClP2KW4ZGAK`; XFlow is repo `sX0BTOBWJX`, parent `meQ-Acjg0K09Xr`.

## CLI transport and persistence

1. `CliTransport` uses argv arrays, an explicit timeout, JSON stdout parsing, bounded stderr, non-secret environment exports, and redacted diagnostic argv. Redact credential flags and large/sensitive content flags (`--token`, `--content`, `--detail`, `--operations`, `--operation`, files containing credentials); never persist environment values or raw stderr that may contain them.
2. All external writes call `StateStore.intent` before execution and `StateStore.receipt` after verified remote evidence. If a result is unknown, leave the intent pending so `Recovery` returns `QUERY_REQUIRED`; do not repeat the write.
3. `KnowledgeSync.publish_phase` creates/verifies one immutable child, updates/verifies the root index, posts/verifies one idempotent iCafe comment, and stores canonical `ku:`/`icafe:` evidence references before reporting success.
4. Any missing receipt, content/version mismatch, duplicate conflict or failed iCafe comment keeps the phase incomplete and returns a stable reason code.

## Tests and boundaries

- Use fake transports only. Cover command timeout, invalid JSON, iCafe business-code failure, KU create/query/edit/publish failure, redacted diagnostics, duplicate/unknown-result comments, unreachable status, immutable child conflict, root index verification, intent-before-write and crash recovery.
- Do not perform any live iCafe/KU write, BGW/XFlow build/test, or production worktree operation.
- Do not add Codex support; the host/approval channels remain Comate and Infoflow.
