# Changelog

All notable changes to this suite. Dates are the dates the work landed locally; entries
before 2026-08-27 are reconstructed from design documents and file timestamps, since the
suite had no version control before then.

## 2026-09-17 — 控制面加固基线收绿 (Phase 0)

把 2026-09-07 的加固工作(iCode/iPipe/KU 运行时客户端、提交描述符、iPipe watcher、
审批投递/摘要/watch、阶段文档渲染、profile 重钉、run brief、stage 参数等)收尾到全绿
基线,并修复独立评审发现的两个 CONFIRMED 问题:

- `submit_descriptor` 在查 worktree ownership 前先解析 profile 仓库路径。此前软链根目录
  (macOS `/var`→`/private/var`)会在 `build_and_archive` 已提交评审字节之后仍报
  `WORKTREE_NOT_OWNED`,导致 CLI `submit` 卡死。
- `stage_parameters.redacted()` 改为按 `IREPO-TOKEN:` 头的位置脱敏,不再依赖 UUID 形状
  正则——非 UUID 的 irepo token 不再泄漏进 G8 审批绑定与证据。
- 对齐测试计数,去掉不实的“不再产生 ResourceWarning”表述。

## 2026-09-07

The x86bgw CDN-URL requirement (iCafe `BGW-1956`) was the suite's first end-to-end run. It
finished, but only because an operator worked around the control plane repeatedly, and this
entry is mostly an account of what those workarounds were covering for. The working-tree
state the run actually used is committed first as a single baseline, so everything below
reads as a diff against what ran rather than against the pre-run skeleton. 720 tests pass.

### Added

- The baseline the run used, as one commit: the Infoflow bot gateway and its reply consumer,
  approval delivery/summary/watch, the iPipe watcher, the submit descriptor that makes SUBMIT
  reachable at all, the phase document renderer, profile repinning and the run brief — plus
  the phase-protocol, iCode-runtime and knowledge-sync fixes those exposed.
- Three operator commands for moves the run had to make by hand. `advance` performs a state
  transition, reading `input_hash` and `approval_id` from the approved ledger row for that
  gate — newest wins, so a reissued gate supersedes one that timed out; the run had been
  driving the state machine from `python3 -c` with a pasted hash, mistyped once. The
  evidence list stays the operator's assertion, because those are evidence names rather
  than artifact kinds and deriving them would produce a gate that reads as though it had
  checked the archive. `artifact show` reads an archived artifact by id *through* the hash
  check, where the run had guessed paths under `artifacts/` and `cat`-ed them past it.
  `abandon-intent` releases an external write whose outcome nobody can ever establish, by
  writing an abandonment receipt instead of the `DELETE FROM external_intents` the run ran
  against the live database; the run summary counts abandonments separately so G10 cannot
  read one as a clean external write. It is deliberately not approval-gated — the intents
  that strand a run are often the approval deliveries themselves.
- `SUBMIT -> DIAGNOSE`. The platform's own review (小码哥) and human CR comments both arrive
  after SUBMIT by design, and `IPIPE` was the only exit, so a confirmed defect there left
  the choice between building code somebody had just called wrong and discarding a run
  holding seven approved phases. The edge goes to DIAGNOSE and not IMPLEMENT, so the finding
  is still root-caused before anything changes; the repair returns through PLAN/SPEC as a
  new revision on the same CR.
- `classification` — `CONFIRMED` / `REJECTED_WITH_REASON` / `NEEDS_CLARIFICATION` — required
  on every Review finding, with a `disposition_reason` required for the two non-confirmed
  values, no blocking rejections, and no `ACCEPT` verdict over an unanswered question. The
  skill had demanded this classification since 2026-08-11 while `review.schema.json` had
  nowhere to record it, so the SUBMIT gate could only see `blocking` and "do not implement
  an unverified suggestion" was unenforceable. An unclarified finding now stops the run with
  `REVIEW_NEEDS_CLARIFICATION` rather than going to DIAGNOSE, which root-causes failures and
  would have to invent the verification the reviewer said it could not produce.
- `deviations` required on every change set — required so that an empty list is the assertion
  that the plan was followed, not a field nobody filled in. It rides `candidate_hash` like
  every other field, so a deviation cannot appear after G5 or a Review bound that hash. The
  G5 card now counts the deviations and names their reasons, which is the one thing an
  approver cannot reconstruct from the plan they approved at G4.
- `submit-descriptor.schema.json`, and a `DELIVERY_FAILED` approval state distinct from
  `PENDING`, so a gate whose card reached nobody says so instead of waiting out a deadline.
- `references/failure-taxonomy.md` now separates the three outcomes of an external write:
  nothing was sent, outcome unknown, redrivable. Four of the bugs below were one mistake —
  reading "I do not know what the remote side did" as "a human has to come and rescue this"
  — and `retry_allowed` now states which class a failure is in rather than how the caller
  feels about waiting.

### Fixed

- A pending KU publish intent wedged the run in `RECOVERY_REQUIRED` with the reconciliation
  sitting behind the guard that refused it. Every KU publish step is keyed on the artifact's
  own content hash, so calling `publish_phase` again re-attempts exactly the write that is
  open — unlike `icode.submit` or `ipipe.trigger`, where the only way to learn the outcome is
  to ask. The CDN-URL run hit this nine times, each time when KU's read-back lagged its own
  successful write, and the only way out was calling `publish_phase` by hand. `complete` now
  excludes the operations it re-drives itself; a publish intent naming a *different*
  artifact still refuses absolutely, and so does the post-publish guard.
- Loosening that guard exposed a latent race: the loser of two concurrent `complete` calls
  was refused by `validate_result` before publishing and reported `STALE_ACTION` for an
  action that had in fact completed under the same key. Reconciled at all three points where
  staleness surfaces.
- A dead approval channel aborted the delivery loop, so Infoflow was never attempted and the
  request reached nobody in either place. Every channel is attempted now, and a channel that
  already delivered short-circuits on retry so nothing is double-posted. A defect in this
  process — a missing method, a signature that does not match — withdraws its own claim and
  returns a retryable `APPROVAL_DELIVERY_FAILED`, while a transport error still leaves the
  claim open to be reconciled; the failure is recorded on the ledger row, so an unanswerable
  gate drops out of the reply watcher and can be reissued.
- Artifacts archived through the plain `ArtifactStore.put` were hashed, indexed and read back
  as evidence with nobody having looked at their shape — including the submit descriptor
  iCode is asked to take and the run summary G10 reasons from. A descriptor missing
  `revision_set` was found by iCode rejecting the submission, one approved G7 gate and one
  push attempt later. Rejection now happens before any write, so nothing can cite an
  artifact that was refused: no file, no index row, no id.
- `run-summary.schema.json` and `optimization-proposal.schema.json` described documents that
  have never existed — a required `result` and `phase_timings`, a flat `target_files` of
  relative paths. Nothing loaded them, so the drift cost nothing and their fixtures tested
  the fiction. Both are rewritten from the builders in `scripts/run_summary.py` and both are
  now loaded. `_validate_optimization_targets` went with them; as written it would have
  called every genuine candidate forbidden.
- `with sqlite3.connect(...) as connection` ends the transaction and leaves the handle open
  until the collector gets to it, and each store opens one per method, so a run leaked a
  descriptor per call and the suite printed a wall of `ResourceWarning`s that hid real
  output. Fixed at each class's single `_connect` factory, which leaves all ~40 call sites
  unchanged and subsumes the two that had grown a hand-rolled `try/finally` around this same
  bug.
- The submission frontier joined on "some change set of this task was submitted", so a
  repaired task looked settled by its old receipt and the run could reach IPIPE on a
  sibling's submission with the repair still in the worktree. It now joins on the task's
  current change set, and `artifacts_for_run` breaks a `created_at` tie on `rowid` rather
  than a content hash, so "the last descriptor" means the one last written.

## 2026-08-27

### Changed

- Collected the 13 previously sibling skill directories under this single
  `tom-autodev-suite/` container. Host skill discovery is flat, so the three host
  directories (`~/.comate/skills`, `~/.codex/skills`, `~/.claude/skills`) keep flat
  symlinks by the same names, now pointing one level deeper. 38 links repointed and
  verified; `install_links.py --dry-run` reports 38 `UNCHANGED`.
- Updated the three hardcoded paths that referenced the old flat location:
  `tom-autodev/tom-autodev-ku.md`, `tom-autodev/tom-autodev-share.md`, and the embedded
  markdown inside `tom-autodev/ku-how-to-use-operation.json`.

Project profiles were untouched: `language_skill` and `project_skill` point at the host
symlink paths, which did not change. `validate_profile` on the bgw profile still returns
`READY`.

### Fixed

- Four test modules failed at import with `ModuleNotFoundError: No module named 'scripts'`
  because they imported sibling test modules as `scripts.tests.X` while the suite has no
  `scripts` package and each module already puts `scripts/` on `sys.path`. `unittest`
  recorded the import errors and skipped the modules, so 96 tests had never run. Switched
  the five imports to the plain sibling form already used by `test_fake_e2e.py`. Collected
  test count went from 341 to 437; all newly enabled tests pass.
  Affected: `test_knowledge_sync.py`, `test_phase_protocol.py`,
  `test_phase_protocol_repair.py`, `test_task7_controller_boundary.py`.
- `test_task5_safety.test_real_gateway_client_orchestrator_chain_accepts_pending_and_nested_reply_once`
  pinned `now` to an absolute `2026-08-11` and set the approval deadline 24 hours later.
  The injected gateway clock honored that, but `ApprovalLedger.receive` judges deadlines
  against the real clock, so once the wall clock passed 2026-08-12 the approval resolved to
  `TIMEOUT` instead of `APPROVE`. Made the test's `now` relative to the current time. The
  production timeout behavior is correct and was not changed.

### Added

- `README.md` and this changelog.

## 2026-08-13

### Changed

- Control-plane implementation reached its current shape: write-once artifact store with a
  SQLite phase index, KU knowledge sync with receipt verification, G0-G10 approval ledger
  with dual-channel first-valid-response semantics, worktree ownership reconciliation, and
  iCode/iPipe runtimes pinned to a validated project profile.

## 2026-08-11

### Changed

- Phase skills (`tom-grill`, `tom-spec`, `tom-tasks`, `tom-plan`, `tom-implement`,
  `tom-review`, `tom-diagnose`) aligned on the shared `ArtifactEnvelope` contract: every
  phase consumes and emits a schema-validated, content-hashed envelope, persists changed
  documents to KU, and links the artifact from an iCafe comment.

## 2026-08-10

### Added

- Executable control-plane design and implementation plan
  (`tom-autodev/docs/superpowers/`): explicit state machine, evidence gate before every
  side effect, failure taxonomy with bounded repair rounds, and post-run G10 optimization
  restricted to approved suite roots.

## 2026-07-15

### Added

- Initial suite: `tom-autodev` controller, `setup-tom-autodev` registration flow, the
  language skills (`tom-lang-c-cpp`, `tom-lang-npl`) and the project skills
  (`tom-project-bgw`, `tom-project-xflow`).
