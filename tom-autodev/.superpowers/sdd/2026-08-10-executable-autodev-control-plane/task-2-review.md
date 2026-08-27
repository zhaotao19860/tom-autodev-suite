# Task 2 Review: Deep Project Profiles and Discovery

## Verdict

- Specification compliance: **FAIL**
- Code quality: **FAIL**
- Critical findings: 1
- Important findings: 5

This review was static. It did not rerun the Task 2 or full-suite tests recorded in `task-2-report.md`.

## Critical Findings

### C1. Approval handling still authorizes Codex and does not enforce the configured Comate/Infoflow channels

The Task 2 contract explicitly limits the approval host to Comate plus Infoflow and says not to add Codex support. `Orchestrator.start()` validates a profile but never retains or supplies its `approval_channels` to approval operations ([scripts/orchestrator.py:25](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:25)). `ApprovalLedger.resolve()` accepts any `channel` string; it neither checks that the channel is configured nor that it is contained in the approval's requested channel list ([scripts/approval_ledger.py:69](/Users/tom/Desktop/skills/tom-autodev/scripts/approval_ledger.py:69)). Therefore a caller can resolve a Comate/Infoflow approval through `codex` or another arbitrary name.

The current tests actively encode that forbidden behavior ([scripts/tests/test_orchestrator.py:106](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_orchestrator.py:106)), and the operational Skill and registry references still name Codex ([SKILL.md:31](/Users/tom/Desktop/skills/tom-autodev/SKILL.md:31), [references/project-registry.md:14](/Users/tom/Desktop/skills/tom-autodev/references/project-registry.md:14)). This is a control-plane authorization failure, not merely stale wording.

## Important Findings

### I1. `test_repo` can be a directory inside the business repository and still pass as independent

The only independence check compares the raw configured strings ([scripts/project_registry.py:116](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:116)). The Git check then only establishes that the configured directory is *inside* a worktree ([scripts/project_registry.py:146](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:146)). A profile with `business_repos[0].path=/work/business` and `test_repo.path=/work/business/tests` passes both checks despite using one worktree; symlink and lexical aliases have the same issue. Resolve each repository to its Git top-level (and canonical path) and reject an identical worktree.

No test covers a nested, aliased, or symlinked test repository. The happy-path test only initializes two separate directories ([scripts/tests/test_project_registry.py:121](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_project_registry.py:121)).

### I2. Approval-channel validation accepts malformed non-empty objects

The schema only enforces `minProperties: 1` for `comate` and `infoflow` ([schemas/project-profile.schema.json:81](/Users/tom/Desktop/skills/tom-autodev/schemas/project-profile.schema.json:81)). It does not require a `channel` field or constrain its type/non-empty value. For example, both `{"channel": ""}` and `{"channel": []}` pass `validate_profile(..., check_paths=False)`, although neither is a non-empty approval channel. Define required, typed channel-reference fields with `minLength: 1` and reject unknown keys. The one negative test exercises only an empty object ([scripts/tests/test_project_registry.py:95](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_project_registry.py:95)).

### I3. KU validation is structural only and accepts arbitrary providers

`knowledge_sources[*].provider` permits any non-empty string ([schemas/project-profile.schema.json:109](/Users/tom/Desktop/skills/tom-autodev/schemas/project-profile.schema.json:109)); no semantic validation verifies that the declared repository and parent ID are KU references. A profile with `provider: "other"`, fabricated `repo_id`, and fabricated `parent_doc_id` is `READY` when paths are disabled. This does not meet the required KU repo/parent-ID validation. Restrict the provider to the supported KU value(s), or dispatch provider-specific validation that rejects non-KU sources where KU metadata is required.

### I4. Discovery does not bind configured test mappings to the discovered Git identity, and ambiguity loses to missing candidates

`discover_candidates()` takes every entry at `mappings["test_repositories"]` without matching it to the root remote/revision ([scripts/profile_discovery.py:27](/Users/tom/Desktop/skills/tom-autodev/scripts/profile_discovery.py:27)). A shared mapping containing a test repository for another project becomes a valid candidate for this project. This is not directory-name guessing, but it is also not identity-based configured mapping discovery.

Further, the result chooses `PROJECT_NOT_READY` whenever *any* group is empty before considering ambiguity ([scripts/profile_discovery.py:36](/Users/tom/Desktop/skills/tom-autodev/scripts/profile_discovery.py:36)). Thus two iCode candidates plus no iPipe candidate returns `PROJECT_NOT_READY`, contrary to the unconditional requirement that ambiguous candidates return `PROFILE_CONFIRMATION_REQUIRED`. The tests cover only a single-category ambiguity where all other categories are present ([scripts/tests/test_profile_discovery.py:47](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_profile_discovery.py:47)).

### I5. Error-list ordering is not deterministic for nested permitted objects

Schema property iteration is stable, but secret paths are traversed in the caller's dictionary insertion order ([scripts/project_registry.py:166](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:166)) and are appended directly to `invalid` ([scripts/project_registry.py:32](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:32)). `approval_channels.comate` and `infoflow` allow arbitrary properties, so two semantically identical profiles that differ only in YAML key order can yield different `invalid` ordering for nested secret-like keys. Sort canonical field paths before returning `missing` and `invalid` (or sort object keys during all recursive traversals). The current test proves a single nested repository order only ([scripts/tests/test_project_registry.py:84](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_project_registry.py:84)); it does not cover this case.

### I6. Confirmed overwrite is not an atomic compare-and-save operation

`save_profile()` hashes existing bytes, validates the new profile, then writes directly to the same path ([scripts/project_registry.py:77](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:77), [scripts/project_registry.py:89](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:89)). Another writer can change the file between the hash comparison and `write_text`, letting this call overwrite content that was not the confirmed version. It can also leave a truncated profile on interruption. Use a lock or platform-supported atomic compare/write sequence, write a temporary file, fsync as appropriate, and atomically replace after rechecking the expected hash. The test establishes stale-hash behavior only without concurrency or interruption ([scripts/tests/test_project_registry.py:156](/Users/tom/Desktop/skills/tom-autodev/scripts/tests/test_project_registry.py:156)).

## Checks That Pass

- The public interfaces requested by Task 2 exist.
- Validation loads the declared schema and reports nested repository fields.
- Default and explicit profile paths both use `config/projects` ([scripts/project_registry.py:18](/Users/tom/Desktop/skills/tom-autodev/scripts/project_registry.py:18), [scripts/orchestrator.py:25](/Users/tom/Desktop/skills/tom-autodev/scripts/orchestrator.py:25)).
- Repository and Skill checks are read-only, and valid Git worktrees plus `SKILL.md` are checked when `check_paths=True`.
- Profile secret values are redacted in validation results; no direct secret-value logging was found in the reviewed Task 2 code.
- The setup Skill correctly documents the `config/projects` layout, confirmation/hash workflow, explicit mapping-only discovery, and Comate/Infoflow names ([setup-tom-autodev/SKILL.md:10](/Users/tom/Desktop/skills/setup-tom-autodev/SKILL.md:10)).

## Test Assessment

The recorded 30 focused and 63 full-suite passes demonstrate the implemented paths, but the suite misses the six cases above. In particular, add regression tests for arbitrary approval resolution, empty/wrongly typed channel values, same-worktree test repositories, non-KU providers, mapping identity selection and mixed missing/ambiguous candidates, stable ordering across reordered mapping keys, and concurrent/interrupted overwrite behavior.
