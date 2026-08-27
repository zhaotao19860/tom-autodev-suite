# Task 9: Update Stage, Language and Project Skills

## Files

- Modify: `/Users/tom/Desktop/skills/tom-autodev/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-grill/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-spec/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-tasks/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-plan/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-implement/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-review/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-diagnose/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-lang-c-cpp/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-lang-npl/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-project-bgw/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/tom-project-xflow/SKILL.md`
- Modify: `/Users/tom/Desktop/skills/setup-tom-autodev/SKILL.md`
- Create/modify: referenced phase templates and checklists under each Skill's `references/`
- Test: `/Users/tom/.codex/skills/.system/skill-creator/scripts/quick_validate.py` for every modified Skill directory

## Required steps

1. Add the Comate-only boundary and embedded-runtime rule. Remove Codex and standalone `tom-autorelease` references from trigger/workflow text.
2. Add phase input/output protocol references. Each phase Skill must list required ArtifactEnvelope inputs, specialized output schema, KU persistence, iCafe comment, approval gate and stop conditions.
3. Add C/C++ and NPL concrete review/test checklists. Include source-review categories, external test interface, fixtures, failure classification and iPipe-only execution rules.
4. Add project-specific test/environment/knowledge graph requirements. Ensure BGW and XFlow profiles cannot infer test repositories or tool limits.
5. Run quick validation for every changed Skill directory and fix all failures.

## Binding global constraints

- Comate is the only supported host; do not add Codex compatibility or a second entrypoint.
- `tom-autorelease` is source material only; the Skill/runtime must not import or execute it.
- Mac may inspect source, generate source/tests, manage worktrees, run source Review, and parse remote evidence only.
- Never run BGW/XFlow compilation, unit, regression, integration, Docker, NCS, simulator, or release commands locally.
- iCafe requirement正文 is immutable; phase links are comments and status transitions.
- Every phase artifact is versioned, content-hashed, schema-validated, published to KU, and linked back to iCafe.
- G0-G10 approval hashes are canonical; G10 is mandatory before applying any optimization.
- Keep skills language/project extensible through child skills and knowledge-graph context.
- Do not initialize Git or fabricate commits in `/Users/tom/Desktop/skills`.
- Do not make live iCafe/KU/Infoflow/iCode/iPipe writes while validating Skills.

## Existing contracts to document, not weaken

- Parent `tom-autodev` exposes Comate-only `start`, `next`, `complete-phase`, `optimize`/G10 and remote iPipe handoff; no local compile/test/release.
- Phase names are `GRILL`, `SPEC`, `TASKS`, `WORKSPACE`, `PLAN`, `IMPLEMENT`, `REVIEW`, `DIAGNOSE`, `SUBMIT`, `IPIPE`, `RELEASE_SUCCESS`/terminal outcomes.
- Stage Skills must persist modified requirement/design/implementation/review/diagnosis documents via the existing KU KnowledgeSync boundary and comment iCafe.
- C/C++ and NPL language Skills provide concrete review/test generation rules, but execution is iPipe-only.
- BGW profile is C/C++; XFlow profile is NPL/TD5 with chip/resource knowledge-graph and NCS/simulator constraints on the remote runner only.

## Report contract

Append a complete Task 9 implementation report to `/Users/tom/Desktop/skills/tom-autodev/.superpowers/sdd/2026-08-10-executable-autodev-control-plane/task-9-report.md` including changed Skills/templates, validation commands and exact per-directory outputs, scans for Codex/tom-autorelease/local execution, hashes, and concerns. Return only status, validation summary, and concerns.
