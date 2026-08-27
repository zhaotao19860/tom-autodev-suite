# Task 2 Report: Deep Project Profiles and Discovery

## Status

DONE

## Delivered

- Added schema-backed profile validation with deterministic `missing` and `invalid` paths.
- Enforced complete repository, skill, knowledge-source/KU, source-only Review, iPipe, environment, and Comate/Infoflow role configuration.
- Rejected secret-bearing keys and redacted their values from validation results.
- Added read-only Git worktree checks and `SKILL.md` checks when `check_paths=True` (the production default).
- Added guarded profile writes: all creates require confirmation; overwrites also require the exact current content hash.
- Added injected-client discovery based only on Git identity and supplied mappings. Missing candidates return `PROJECT_NOT_READY`; multiple candidates return `PROFILE_CONFIRMATION_REQUIRED`.
- Unified profile lookup under `~/.tom-autodev/config/projects/<project>.yaml` and `<explicit-config-root>/config/projects/<project>.yaml`.
- Updated setup guidance to match the executable contract and approval hosts.

## TDD Evidence

- Initial registry RED: `save_profile` import failed because the interface did not exist.
- Added path-save test RED: an unavailable repository was incorrectly accepted for saving.
- Added worktree test RED: a directory containing only a `.git` marker was incorrectly accepted.
- Added discovery test RED: an unconfigured product-test repository was incorrectly reported as confirmation-needed rather than not ready.
- Each regression was implemented minimally and rerun green.

## Changed Files

- `scripts/schema_validator.py` (new)
- `scripts/profile_discovery.py` (new)
- `scripts/project_registry.py`
- `scripts/orchestrator.py`
- `schemas/project-profile.schema.json`
- `scripts/tests/test_project_registry.py`
- `scripts/tests/test_profile_discovery.py` (new)
- `scripts/tests/test_orchestrator.py`
- `/Users/tom/Desktop/skills/setup-tom-autodev/SKILL.md`

`test_orchestrator.py` required a fixture update because the new, correct runtime path is `config/projects`; its fixture now also creates temporary valid Git repositories and Skills rather than disabling production path validation.

## Verification

From `/Users/tom/Desktop/skills/tom-autodev`:

```text
python3 -m unittest scripts/tests/test_project_registry.py scripts/tests/test_profile_discovery.py scripts/tests/test_orchestrator.py -v
Ran 30 tests ... OK

python3 -m unittest discover -s scripts/tests -v
Ran 63 tests ... OK

python3 -m json.tool schemas/project-profile.schema.json >/dev/null
exit 0
```

No BGW/XFlow builds, pipeline activity, live discovery calls, profile writes outside temporary test directories, or business-workspace changes were performed.

## Concerns

None.

## Fix Round 1: Review Remediation

### Status

DONE

### Delivered

- Approval requests now require exactly the `comate` and `infoflow` logical channels. Resolution rejects any channel not requested/configured before it can affect the first-valid decision.
- Approval-facing Skills, references, adapters, and tests now name Comate and Infoflow only.
- Profile approval channel references require an exact non-empty string `channel` property and reject unknown keys.
- Knowledge providers are an explicit enum (`ku`, `gitnexus`, `repository`). At least one KU source is mandatory and only KU sources require non-empty `repo_id` and `parent_doc_id`; supported supplemental sources remain representable.
- Test-repository independence now compares canonical Git worktree top-levels, rejecting nested directories and symlink aliases of business repositories.
- Discovery filters configured test mappings by discovered source remote and optional matching revision. Ambiguity wins over missing candidate groups.
- Readiness paths are sorted, unique, canonical field paths.
- `save_profile` now takes an advisory exclusive lock, rechecks the expected hash while locked, writes and fsyncs a temporary file, atomically replaces the target, fsyncs the parent directory, and removes temporary content after interruption.

### TDD Evidence

- RED: unsupported/unrequested approval channel resolution was accepted.
- RED: malformed channel references, arbitrary/missing-KU knowledge sources, same-worktree test repositories, unmatched mappings, mixed missing/ambiguous discovery, and unordered paths were all accepted or misclassified.
- RED: interrupted replacement could not be tested because saves used a direct write, and the first concurrent test clarified that identical writes are idempotent. The final regression uses two distinct confirmed payloads and proves one `READY` plus one `PROFILE_CONFLICT` result.
- GREEN: all above regressions pass with the production changes.

### Verification

From `/Users/tom/Desktop/skills/tom-autodev`:

```text
python3 -m unittest scripts/tests/test_gates.py scripts/tests/test_project_registry.py scripts/tests/test_profile_discovery.py scripts/tests/test_orchestrator.py scripts/tests/test_adapters.py -v
Ran 61 tests ... OK

python3 -m unittest discover -s scripts/tests -v
Ran 73 tests ... OK

python3 -m json.tool schemas/project-profile.schema.json >/dev/null
exit 0
```

The approval-facing source scan (`SKILL.md`, `references/`, and `scripts/tests/`) found no obsolete host naming. No business workspace, pipeline, or live external service was used.

### Additional Changed Files

- `scripts/approval_ledger.py`
- `scripts/tests/test_gates.py`
- `scripts/tests/test_adapters.py`
- `tom-autodev/SKILL.md`
- `references/approval-policy.md`
- `references/external-contracts.md`
- `references/project-registry.md`
