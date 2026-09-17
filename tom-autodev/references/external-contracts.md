# External Contracts

All external adapters accept injected transports. Unit tests use fake transports and never call live services.

## iCafe

`snapshot(card_id)` returns card ID, title, body, acceptance criteria, attachment pointers, and canonical `content_hash`. Human confirmation is required before binding the snapshot to a run.

## iCode

`submit(revision_set)` requires matching current and approved input hashes. It returns CR IDs, revisions, target branches, and a stable Revision Set ID. CR creation must use the iCode `git push_cr --repo-path ... --branch ...` boundary; never fall back to raw Git push.

## iPipe

`trigger(profile, revision_set)` requires a stable pipeline ID and explicit parameter allowlist. Only allowed revision/test/environment parameters are sent. `rerun` requires G8 approval.

## Review

`review(change_set)` returns independent Standards and Spec axes. Timeout, missing provider, or empty output returns `INCOMPLETE`, never `PASS`.

## Approval Channels

Publish identical approval ID, evidence summary, and input hash to Comate and Infoflow. A channel delivery failure is explicit and auditable; do not silently reduce dual-channel approval to one channel.
