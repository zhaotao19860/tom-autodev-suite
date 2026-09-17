# tom-autodev-suite

A Comate skill suite that drives one iCafe requirement from clarification to release
through a hash-bound, approval-gated control plane. The controller owns every external
adapter and gate; child skills only produce schema-validated artifacts and evidence.

The design premise: the Mac side never produces project execution evidence. Compilation,
unit tests, regression, integration and release all happen on the configured iPipe runner.
Any local build request returns `LOCAL_EXECUTION_FORBIDDEN`.

## Layout

This directory is a container, not a skill. Each subdirectory is an independently
discoverable skill with its own `SKILL.md`.

| Skill | Role |
|---|---|
| `tom-autodev` | Control plane, state machine, G0-G10 gates, iCafe/KU/iCode/iPipe adapters |
| `setup-tom-autodev` | One-time supervised project registration, writes the project profile |
| `tom-grill` | Clarifies ambiguous requirements into a verified decision map |
| `tom-spec` | Versioned behavioral Spec, test interfaces, acceptance traceability |
| `tom-tasks` | Decomposes the Spec into an acyclic DAG of verifiable capability slices |
| `tom-plan` | Per-task implementation plan with exact files, symbols and test cases |
| `tom-implement` | Generates coordinated business-repo and product-test patches |
| `tom-review` | Independent Standards and Spec review over a fixed diff baseline |
| `tom-diagnose` | Classifies failures and routes repair versus environment decisions |
| `tom-lang-c-cpp`, `tom-lang-npl` | Language-specific generation, review and test design |
| `tom-project-bgw`, `tom-project-xflow` | Repository topology, environment and pipeline rules |

## Workflow

```text
Intake -> tom-grill -> tom-spec -> tom-tasks -> WorkspaceGate
       -> tom-plan -> tom-implement -> EvidenceGate -> tom-review
       -> iCode -> iPipe -> approved release
```

One frontier task advances at a time. `EvidenceGate` runs before every checkpoint and
every external side effect, blocking on a changed input hash, missing artifact,
unresolved finding, revision mismatch, or stale environment fingerprint.

`tom-review` produces evidence only. A dual-axis PASS is a prerequisite for the
controller-owned G7 approval, never a substitute for it.

## Approval gates

| Gate | Approval object |
|---|---|
| G0 | iCafe card and project binding |
| G1 | each human decision from `tom-grill` or architecture Review |
| G2 | Spec, test interface, environment requirements |
| G3 | end-to-end Task DAG |
| G4 | each task's Task Plan |
| G5 | each task's complete candidate diff |
| G6 | each repair diagnosis, repair plan, and repair diff |
| G7 | iCode submission or patchset |
| G8 | iPipe failed-stage rerun or manual-stage continuation |
| G9 | release evidence and release action |
| G10 | post-run skill optimization candidate diff |

The same `approval_id`, evidence summary and `input_hash` go to both Comate and Infoflow.
The first valid response wins; later responses are audited without changing the effective
decision. An approval expires as soon as its bound input changes. Default wait is ten
hours, after which the run stops.

## Install

Skill discovery is flat: each host scans `skills/<name>/SKILL.md` one level deep. The
suite therefore stays a source-side container while the hosts get flat symlinks.

```bash
cd tom-autodev/scripts
python3 install_links.py \
  --root /Users/tom/Desktop/skills/tom-autodev-suite \
  --destination ~/.comate/skills \
  --destination ~/.codex/skills \
  --destination ~/.claude/skills \
  --dry-run
```

Drop `--dry-run` to create the links. The command reports `CREATE`, `UNCHANGED`,
`CONFLICT` or `MISSING_SOURCE` per target and exits non-zero on the last two.

## Usage

Register a project once, then drive runs through the CLI.

```bash
cd tom-autodev
python3 scripts/cli.py preflight <project>          # read-only availability check
python3 scripts/cli.py start <icafe-card> <project> # create snapshot and run record
python3 scripts/cli.py status <run-id>
python3 scripts/cli.py next <run-id>
python3 scripts/cli.py complete-phase <run-id> <artifact-envelope.json>
python3 scripts/cli.py approve <approval-id> approve <input-hash> comate <run-id> <user>
python3 scripts/cli.py resume <run-id>
python3 scripts/cli.py advance <run-id> <state> --artifact NAME   # gate hash from the ledger
python3 scripts/cli.py artifact show <artifact-id>                # read back, hash-checked
python3 scripts/cli.py abandon-intent <intent-id> --reason R --actor A
```

`advance` takes `input_hash` and `approval_id` from the approved ledger row for the
transition's gate, so neither is pasted by hand. The `--artifact` names stay the
operator's assertion; omit them and the gate answers `MISSING_ARTIFACT` with what it
wants. `abandon-intent` releases an external write whose outcome will never be known by
writing an abandonment receipt — it never deletes the intent. See
[`references/failure-taxonomy.md`](tom-autodev/references/failure-taxonomy.md).

`preflight` only queries availability. It never creates a group, submits code, or triggers
a pipeline. A missing requirement returns `PROJECT_NOT_READY`.

## Where state lives

| What | Where |
|---|---|
| Project profiles | `~/.tom-autodev/config/projects/<project>.yaml` |
| Run state, approvals | `~/.tom-autodev/state.sqlite`, `approvals.sqlite` |
| Phase artifacts | `~/.tom-autodev/artifacts/<run_id>/<phase>/<artifact_id>/content.bin` |
| Artifact index | `~/.tom-autodev/artifacts/artifact-index.sqlite` |
| Task worktrees | `<repo-parent>/.<repo>-tom-autodev-worktrees/` |

Artifacts are write-once. A changed phase output becomes a new `artifact_id` and a new
`phase_artifacts` row rather than overwriting the previous version, so every version stays
inspectable. The same content is mirrored to the KU knowledge base, one child document per
phase, with the receipt (`knowledge_doc_id`, `knowledge_url`, `knowledge_version`,
`icafe_comment_id`) written back into the artifact envelope.

Code diffs live inside the IMPLEMENT change-set envelope as `business_patch` and
`test_patch` with a `full_diff_hash`. Diagnoses store only `repair_diff_hash`.

## Tests

```bash
cd tom-autodev/scripts
python3 -m unittest discover -s tests -q
```

751 tests, no network access required. Run them from `scripts/` so sibling test modules
resolve.

## Boundaries worth remembering

- No local project build, test, regression, Docker or NCS invocation, and no local substitute.
- No secret persistence; `persistence_policy` rejects any artifact carrying a secret-shaped key.
- No automatic CR merge, no raw `git push` as an iCode fallback, no runtime edit of pipeline templates.
- G10 may only touch approved tom-autodev roots; business repositories, project profiles and
  pipeline templates are out of reach by construction.
