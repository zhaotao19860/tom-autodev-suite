# Project Registration (Setup)

Register a new project or language before `tom-autodev.start` will process its requirements. Setup is a one-time, human-supervised operation; it does not start a run, generate code, submit iCode, trigger iPipe, or modify pipeline templates.

Comate host only. Do not add a second adapter or entrypoint; release stays a controller action, never a separately registered runtime component.

## Template + per-requirement profile

Stable per-project facts (repository modules/paths, pipeline registration, environment,
approval channels) ship as a template at `tom-autodev/templates/<project>.template.yaml`.
Each requirement gets its own profile derived from that template — one file per card at
`~/.tom-autodev/config/projects/<project>/<CARD>.yaml` — so concurrent requirements never
overwrite one another.

Starting a new requirement therefore only asks for what actually changes; everything else
is carried from the template or derived:

1. **Project** — selects `templates/<project>.template.yaml` (e.g. `bgw`, `xflow`).
2. **Knowledge-base parent** — the KU `repo_id` / `parent_doc_id` / snapshot revision.
3. **Repositories and branches** — the business repo branches and the product-test branch.
4. **Pinned revisions** — the source revision per repository.
5. **Members** — development / test / project owners.

Auto-derived (never asked): each `repository`/`gitnexus` knowledge revision follows its
repo's pinned revision; each `lock`'s last segment follows the branch; `release_rule`'s
`{primary_branch}` follows the primary business branch. iPipe pipelines are discovered as
bounded read-only candidates; the user must confirm the selected IDs. The BGW template's
environment block is descriptive `UNVERIFIED` context until a real platform receipt fills
its provenance; such a profile is deliberately rejected as `READY`.

Run the wizard with an answers file:

```
python3 tom-autodev/scripts/profile_wizard.py --project bgw --card BGW-1995 --workspace-root /path/to/bgw-ref
```

The first command prints the Git checkouts under `--workspace-root` (which defaults to
the current working directory), their current branch/HEAD, and the exact `ChangePipeline`
ID candidates discovered through iPipe. It does not read a separate hardcoded checkout
directory. The user must choose or replace those candidates before preparing
`answers.json`. The answers file holds only the variable parts:
`repository_paths`, `branches`, `test_branch`, `revisions`, `ku`, `pipeline_ids`, and
`members`, and, when environment evidence is available, `environment_provenance`, plus:

- `confirmed_inputs`: the exact effective values after the user confirms or edits them;
- `confirmed_by`: the confirming owner.

Then write the profile:

```
python3 tom-autodev/scripts/profile_wizard.py --project bgw --card BGW-1995 --answers answers.json
```

The wizard compares `confirmed_inputs` with the profile it is about to write, refuses to
write while any `__PLACEHOLDER__` remains, records a confirmation hash, validates, and
writes the per-requirement profile (hash-guarded on rewrite). The requirement start path
requires this per-card profile and confirmation; it no longer silently falls back to the
legacy project-level profile.

## Profile

Collect and validate every field defined in [`project-registry.md`](project-registry.md). Profiles must explicitly state which unit, regression, integration, NCS/simulator, and release stages run remotely; no local fallback is valid.

Store non-secret data at the per-requirement path above; an explicit config root uses its `config/projects/<project>/<CARD>.yaml` child. Keep credentials in existing login files or environment variables; never read or print secret values. Use injected iCode/iPipe discovery clients and configured mappings only — do not guess repositories, pipelines, or emails from directory names. Refuse to create or overwrite a profile without explicit human confirmation; overwrite also requires the exact saved previous content hash.

## Procedure

1. Inspect repository paths, Git revisions, worktree state, read-only knowledge paths, and profile references.
2. Confirm each business/test repository belongs to the same product and has a lock key.
3. Confirm the pipeline is a preconfigured stable template and list its allowed runtime parameters; do not create dynamic stages.
4. Confirm the environment profile declares runner, image/tool versions, hardware/simulator, data, services, and capacity, and attach `provenance.status=VERIFIED`, runner/image-toolchain identity, verification time, verifier, and an evidence reference.
5. Run `project_registry.validate_profile`; return `PROJECT_NOT_READY` with the exact missing fields when incomplete.
6. Save the profile content hash and human setup evidence.

Only a `READY` profile allows `tom-autodev.start`. Reuse a validated profile for later requirements; do not re-register per requirement. BGW uses `tom-lang-c-cpp` + `tom-project-bgw`; XFlow uses `tom-lang-npl` + `tom-project-xflow`.
