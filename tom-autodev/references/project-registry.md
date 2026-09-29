# Project Registry

A project profile must define:

- `project_id`
- `business_repos`: path, iCode module, target branch, lock key
- `test_repo`: independent product-test repository and module
- `language_skill`
- `project_skill`
- `knowledge_sources`: provider, repository/revision, search scope, priority
- `pipeline_profile`: stable pipeline ID, allowed parameters, stage classes, release rule
- `environment_profile`: runner, image, tools, hardware/simulator, data, services
- `approval_channels`: Comate and Infoflow configuration refs

`review_provider` is optional. Review is produced by `tom-review` as a model phase, so a
profile no longer needs to associate an external review command; when the key is absent
preflight skips the review component. A profile may still pin a `source-only` command for
back-compat.

## Where profiles live

Profiles are **per requirement**, not one-per-project: a run pins its own profile at
`~/.tom-autodev/config/projects/<project>/<CARD>.yaml`, so two requirements developed at
the same time never overwrite one another. `requirement_profile_path(project, card)`
derives this path; `resolve_active_profile_path` prefers it and falls back to the legacy
one-per-project `~/.tom-autodev/config/projects/<project>.yaml` for older runs. The
runtime guards accept either the per-requirement or the legacy path for the run's
`(project, card)` and reject any other with `PROJECT_PROFILE_PATH_MISMATCH`.

Stable per-project facts ship as a template at `tom-autodev/templates/<project>.template.yaml`.
`scripts/profile_wizard.py` copies the template, fills only the variable slots from your
answers, derives the dependent ones, validates, and writes the per-requirement profile.
See [`setup.md`](setup.md).

Keep credentials in existing login files or environment variables. Return `PROJECT_NOT_READY` when any required entry is absent or unreadable.
