# Task 2: Deep Project Profiles and Discovery

## Files

- Create `scripts/profile_discovery.py` and `scripts/schema_validator.py`.
- Modify `scripts/project_registry.py` and `schemas/project-profile.schema.json`.
- Modify sibling Skill `/Users/tom/Desktop/skills/setup-tom-autodev/SKILL.md` only as needed for the executable contract.
- Add or update `scripts/tests/test_project_registry.py` and `scripts/tests/test_profile_discovery.py`.

## Interfaces

- `validate_profile(profile: dict, *, check_paths: bool = True) -> dict`
- `discover_candidates(project_root: Path, *, icode: object, ipipe: object, mappings: dict | None = None) -> dict`
- `save_profile(path: Path, profile: dict, previous_hash: str | None, confirmation: bool) -> dict`

## Required behavior

1. Use `schemas/project-profile.schema.json` and deterministic error paths. Validate nested repo path/module/branch/lock, an independent product-test repo, existing language/project Skill paths, a supported source-only Review provider, non-empty Comate and Infoflow approval channels, iPipe pipeline ID and parameter allowlist, complete environment profile, KU repo/parent IDs and role-member configuration.
2. Reject credentials/secrets in profiles. Do not read or print credential values.
3. With `check_paths=True`, verify repository paths exist, are Git worktrees, and the referenced Skill folders contain `SKILL.md`. Checks are read-only.
4. Runtime and setup both use `~/.tom-autodev/config/projects/<project>.yaml`; an explicit config root still owns a `config/projects` child.
5. Discovery reads Git remote/revision and calls injected iCode/iPipe clients. It may use only configured mappings for the independent product-test repository and must not guess from directory names.
6. Ambiguous candidates return `PROFILE_CONFIRMATION_REQUIRED`; profile creation and overwrite require `confirmation=True`, and overwrite also requires the exact previous content hash.
7. Preserve stable `READY`, `PROJECT_NOT_READY`, `PROFILE_CONFIRMATION_REQUIRED`, `PROFILE_CONFIRMATION_REQUIRED`, and conflict reason codes with deterministic missing/invalid lists.
8. Do not add Codex support. Approval host is Comate plus Infoflow.
9. Do not run BGW/XFlow compilation, unit, regression, integration, simulator, Docker, or local substitutes.

## Test sequence

Write failing tests first and demonstrate RED, implement the minimum, then run focused tests and `python3 -m unittest discover -s scripts/tests -v` from the Skill root.

## Environment note

`/Users/tom/Desktop/skills` is not a Git repository. Do not initialize Git, create commits, or alter user business workspaces.
