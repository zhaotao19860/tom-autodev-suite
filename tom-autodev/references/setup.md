# Project Registration (Setup)

Register a new project or language before `tom-autodev.start` will process its requirements. Setup is a one-time, human-supervised operation; it does not start a run, generate code, submit iCode, trigger iPipe, or modify pipeline templates.

Comate host only. Do not add a second adapter or entrypoint; release stays a controller action, never a separately registered runtime component.

## Profile

Collect and validate every field defined in [`project-registry.md`](project-registry.md). Profiles must explicitly state which unit, regression, integration, NCS/simulator, and release stages run remotely; no local fallback is valid.

Store non-secret data at `~/.tom-autodev/config/projects/<project>.yaml`; an explicit config root uses its `config/projects/<project>.yaml` child. Keep credentials in existing login files or environment variables; never read or print secret values. Use injected iCode/iPipe discovery clients and configured mappings only — do not guess repositories, pipelines, or emails from directory names. Refuse to create or overwrite a profile without explicit human confirmation; overwrite also requires the exact saved previous content hash.

## Procedure

1. Inspect repository paths, Git revisions, worktree state, read-only knowledge paths, and profile references.
2. Confirm each business/test repository belongs to the same product and has a lock key.
3. Confirm the pipeline is a preconfigured stable template and list its allowed runtime parameters; do not create dynamic stages.
4. Confirm the environment profile declares runner, image/tool versions, hardware/simulator, data, services, and capacity.
5. Run `project_registry.validate_profile`; return `PROJECT_NOT_READY` with the exact missing fields when incomplete.
6. Save the profile content hash and human setup evidence.

Only a `READY` profile allows `tom-autodev.start`. Reuse a validated profile for later requirements; do not re-register per requirement. BGW uses `tom-lang-c-cpp` + `tom-project-bgw`; XFlow uses `tom-lang-npl` + `tom-project-xflow`.
