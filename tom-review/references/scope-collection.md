# Fixed review scope

Use the pinned Change Set's complete per-repository baseline and candidate commit IDs. When a trusted complete inventory is already supplied, reuse it. Otherwise collect each business/test repository with the bundled helper:

```bash
python3 "<tom-review-base>/scripts/collect_scope.py" \
  --repo "<owned-repository>" --base "<full-baseline-commit>" --head "<full-candidate-commit>"
```

The helper uses Python's standard library and Git. It reads commit objects; it does not fetch, checkout, edit, compile, run tests, or execute external diff/textconv drivers. It also avoids `git status`, which can execute a repository's clean filter. Output goes to stdout; save it with the current job's supporting evidence if needed. No connection to an external Review skill is used.

The inventory includes additions, deletions, type/mode changes and gitlinks; renames deliberately appear as deletion plus addition so no similarity heuristic hides either side. Paths use NUL-safe Git records and JSON escaping, including spaces, tabs and newlines. Binary contents and gitlink internals still need their applicable source/format evidence; a file list is not a completed review. Numstat counts/binary classification are advisory and can depend on Git attributes; never use them as severity or completeness gates.

The working tree is explicitly `not_inspected` and excluded: the commit IDs, not current HEAD or uncommitted files, define this candidate. `worktree_changes_ignored: true` describes this policy, not a detected dirty state. If the approved candidate actually includes uncommitted work, request the parent-owned pinned candidate; do not silently review whatever happens to be on disk. Missing objects or truncated output mean scope is incomplete, not an empty clean change.

Read the pinned diff and relevant caller/delegate context for every listed file. For a large change, maintain a file/AC coverage ledger in supporting evidence and inspect bounded groups; only mark both axes complete after the required scope is covered. Do not mark a skipped binary, generated contract, submodule change or missing repository reviewed by inference. A recorded exclusion needs a concrete scope reason, not just an extension/size threshold.

This helper produces an inventory, not Review DraftContent or a change-set hash. Use the worker-provided identity and the current Review schema for the actual draft. Quality-rule catalogs remain reading guidance; do not claim any scanner ran.
