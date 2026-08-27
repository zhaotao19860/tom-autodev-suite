---
name: setup-tom-autodev
description: Use when a new project or language must be registered before tom-autodev can safely process requirements, generate patches, or use iPipe.
---

# Setup Tom Autodev

## Purpose

Create and validate one project registry profile. Setup is a one-time, human-supervised operation; it does not start a requirement run, generate code, trigger iPipe, or modify pipeline templates.

This setup is for the Comate host only. Do not add a second adapter or entrypoint; release remains a controller action and never a separately registered runtime component.

## Required Profile

Collect and validate `project_id`, business repository paths/modules/branches/locks, independent product-test repository and fixture ownership, language/project child skills, knowledge providers and revisions/graph freshness, external test interfaces, a source-only Review provider, stable iPipe pipeline profile, environment profile, Comate and Infoflow approval channels, and release rule/version mapping. Profiles must explicitly state which unit, regression, integration, NCS/simulator and release stages run remotely; no local fallback is valid.

Store non-secret data at `~/.tom-autodev/config/projects/<project>.yaml`; an explicit config root still uses its `config/projects/<project>.yaml` child. Keep credentials in existing login files or environment variables; do not read or print secret values. Use injected iCode/iPipe discovery clients and configured mappings only. Refuse to create or overwrite a profile without explicit human confirmation; overwrite also requires the exact saved previous hash.

## Procedure

1. Inspect repository paths, Git revisions, worktree state, read-only knowledge paths, and profile references.
2. Confirm each business/test repository belongs to the same product and has a lock key.
3. Confirm the pipeline is a preconfigured stable template and list its allowed runtime parameters; do not create dynamic stages.
4. Confirm the environment profile declares runner, image/tool versions, hardware/simulator, data, services, and capacity.
5. Run `project_registry.validate_profile`; return `PROJECT_NOT_READY` with the exact missing fields when incomplete.
6. Save the profile content hash and human setup evidence. Only a `READY` result allows `tom-autodev.start`.

**REQUIRED PARENT:** Return the validated profile, content hash, readiness result, and evidence references to `tom-autodev`.
