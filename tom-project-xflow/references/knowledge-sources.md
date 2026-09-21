# XFlow Knowledge Sources

- Current source: business repositories and locked revisions from the configured profile.
- Official examples: the profile's read-only reference root, with chip and release compatibility recorded.
- Approved Markdown/PDF: the profile's document root or versioned URL; record document revision and relevant section.
- Historical failures: approved evidence records, including source revision, toolchain, environment, outcome, and unresolved alternatives.
- Merged local guidance: [LT control plane](lt-control-plane.md), [mirror evidence](mirror-evidence.md), and [chip constraints](chip-constraints.md). [Sources](sources.md) records import scope and provenance; no external skill is needed to read this guidance.

Resolve project evidence through the configured profile. Missing paths reduce evidence quality and may return `PROJECT_NOT_READY`; never silently switch to a different checkout. Use source text search if a graph index is missing/stale and record any impact area that remains unverified.

Separate `normative` document rules, `current-confirmed` source/remote facts, `historical-observed` behavior, `hypothesis`, and `disproved-for-recorded-scope`. A disproved hypothesis is rejected only for its recorded experiment conditions. Conflicting historical claims remain unresolved until current evidence distinguishes them; do not promote the more confident wording to a fact.
