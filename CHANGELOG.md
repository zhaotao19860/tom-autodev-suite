# Changelog

All notable changes to this suite. Dates are the dates the work landed locally; entries
before 2026-08-27 are reconstructed from design documents and file timestamps, since the
suite had no version control before then.

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
