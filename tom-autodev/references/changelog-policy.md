---
name: changelog-policy
description: Every workflow task that changes a module's source must add a matching changelog entry, in the module's existing format (bootstrapped from 1.0.0 when absent).
---

# Changelog Policy

Any task that changes source code in a repository/module MUST, in the **same change-set**,
add a changelog entry for that module. This is a required part of IMPLEMENT and a blocking
REVIEW check — a code diff without a corresponding changelog edit is incomplete.

Scope: applies per repository the change-set modifies. The changelog file itself, and
pure non-source artifacts, do not themselves trigger a new entry. When one task changes
several repositories, each changed repository gets its own entry.

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
- Bump from the current top version: a `[Bug]` fix bumps PATCH; a `[Story]`/feature bumps
  MINOR (reset PATCH to 0); keep MAJOR unless the module declares a breaking change.
- `<CARD-ID>` is the iCafe card driving the task (e.g. `BGW-1995`); `<Type>` mirrors the
  card type in brackets (`[Bug]`, `[Story]`), matching the file's existing casing.
- Date is the commit date `YYYY-MM-DD`; `<name> <<email>>` is the committing repository
  identity (the same `user.name`/`user.email` used by `_commit_if_dirty`), never a bot.
- One `-` bullet per card/change; indent bullets with 4 spaces.

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
