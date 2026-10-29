---
name: changelog-policy
description: Every workflow task that changes any file in a repository (business or product-test, code or docs) must add a matching changelog entry, in the repository's existing change-log format (bootstrapped from 1.0.0 when absent).
---

# Changelog Policy

Any task that changes **any file** in a repository MUST, in the **same change-set**, add a
change-log entry for that repository, so every change can be traced from the repository
alone. This holds for business repositories and the product-test repository alike, and for
documentation-only changes (README, cases, configs) as well as code. It is a required part
of IMPLEMENT, a blocking REVIEW check, and enforced by `prepare-change-set`, which refuses
a repository whose commit touches files but no root change log
(`CHANGELOG_ENTRY_MISSING:<role>`).

Scope: per repository the change-set modifies; each changed repository gets its own entry.
A repository the change-set does not touch needs none.

The change-log file is the one at the repository root: `changelog` (x86bgw, bgwagent),
`CHANGELOG.md` (product-test repositories such as `baidu/nsiqa/x86bgw`), or another root
`changes.md` / `history.md` the repository already uses. Never create a second one.

## Standard format

The authoritative format is the module's existing `changelog` file at the repository root.
The reference standard (extracted from `baidu/sysip/x86bgw` `changelog`) is:

```
<MAJOR.MINOR.PATCH> <YYYY-MM-DD> <name> <<email>>
    - <CARD-ID> [<Type>] <description>
```

Rules:
- Newest entry on top (prepend below the file header, above the previous newest version).
- Version string MUST match `[0-9]+\.[0-9]+\.[0-9]+`.
- Bump from the current top version on **every** change-set: a `[Bug]` fix or a
  documentation/test-only change bumps PATCH; a `[Story]`/feature bumps MINOR (reset PATCH
  to 0); keep MAJOR unless the module declares a breaking change.
- `<CARD-ID>` is the iCafe card driving the task (e.g. `BGW-1995`); `<Type>` mirrors the
  card type in brackets (`[Bug]`, `[Story]`), matching the file's existing casing.
- Date is the commit date `YYYY-MM-DD`; `<name> <<email>>` is the committing repository
  identity (the same `user.name`/`user.email` used by `_commit_if_dirty`), never a bot.
- One `-` bullet per card/change; indent bullets with 4 spaces.

## Every entry carries a new version

Every change-set bumps the repository's version, whatever the file format: an entry
without its own version cannot be traced to a build or a CR. `prepare-change-set` refuses
a change log whose first version heading is not greater than the baseline's
(`CHANGELOG_VERSION_NOT_BUMPED:<role>`).

## Keep a Changelog repositories

A repository whose `CHANGELOG.md` follows [Keep a Changelog] (the product-test repository
does) keeps that format, but the entry goes into a **new version section**, never only
under `[Unreleased]`. Insert it below the `[Unreleased]` block and above the previous
newest version, in the file's own heading style, with the bump rule above:

```
[Unreleased]
---
### Added
- 这里记录新添加的内容
### Changed
- 这里记录变更的内容

0.1.1 - <YYYY-MM-DD>
---
### Changed
- <CARD-ID> [<Type>] <description> (<name> <<email>>)

0.1.0 - 2018-10-24
---
```

Leave the `[Unreleased]` template untouched. If the file keeps version link references
at the bottom (`[Unreleased]: .../merge/0.1.0...master`), add the new version's line and
move `[Unreleased]` to start from it.

## When the module has no changelog

Create a `changelog` file at the repository root, seed the header, and start at `1.0.0`:

```
Revision history of <module>.

ATTENTION: version number must in the format of [0-9]+.[0-9]+.[0-9]

1.0.0 <YYYY-MM-DD> <name> <<email>>
    - <CARD-ID> [<Type>] <description>
```

Match the module's own header wording when a sibling convention exists; otherwise use the
line above. Do not invent a different filename or location when the module already has one.
