# Project Registry

A project profile must define:

- `project_id`
- `business_repos`: path, iCode module, target branch, lock key
- `test_repo`: independent product-test repository and module
- `language_skill`
- `project_skill`
- `knowledge_sources`: provider, repository/revision, search scope, priority
- `review_provider`: source-only command or human fallback
- `pipeline_profile`: stable pipeline ID, allowed parameters, stage classes, release rule
- `environment_profile`: runner, image, tools, hardware/simulator, data, services
- `approval_channels`: Comate and Infoflow configuration refs

Store non-secret profile data at `~/.tom-autodev/config/projects/<project>.yaml`. Keep credentials in existing login files or environment variables. Return `PROJECT_NOT_READY` when any required entry is absent or unreadable.
