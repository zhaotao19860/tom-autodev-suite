# Approval Policy

| Gate | Approval object |
|---|---|
| G0 | iCafe card and project binding |
| G1 | each human decision from `tom-grill` or architecture Review |
| G2 | Spec, test interface, environment requirements |
| G3 | end-to-end Task DAG |
| G4 | each task's `Task Plan` |
| G5 | each task's complete candidate diff |
| G6 | each repair diagnosis, repair plan, and repair diff |
| G7 | iCode submission or patchset |
| G8 | iPipe failed-stage rerun or manual-stage continuation |
| G9 | release evidence and release action |

Publish the same `approval_id`, evidence summary, and `input_hash` to Comate and Infoflow. Accept the first valid response. Audit later responses without changing the effective decision. Expire an approval whenever its bound input changes. Default wait limit is ten hours; timeout stops the run.

## Infoflow replies

Both directions belong to the bot app (`zhaotao02_Bot`). A personal account cannot hold the inbound WebSocket, so a request sent as anything else could never be answered automatically:

```text
tom-autodev -> localhost /group/message -> bot gateway -> 如流 (运行协作群)
            -> localhost /notify        -> 如流 (机器人单聊，群还不存在时)
如流 回复 -> bot WebSocket -> infoflow-replies.jsonl
          -> InfoflowReplyConsumer (authorize + bind) -> ApprovalLedger -> run continues
```

Gate cards, reminders, and acknowledgements go to the run's collaboration group when G0 already created one (`collaboration.group_id_for_run` reads the `infoflow.group.create:<run_id>` receipt), so a decision has the same audience as the work it gates; the approvers are `@`-mentioned in the card. Without a group the same card goes to the members' private chats. Progress notices stay private to the owner.

No shell setup is required. `InfoflowBotClient` starts `infoflow-gateway/src/bot_gateway.mjs` on demand (installing `node_modules` on first use) and the gateway resolves the bot credentials itself: `INFOFLOW_APP_KEY`/`INFOFLOW_APP_SECRET` from the environment, otherwise `INFOFLOW_AK`/`INFOFLOW_SK` from `~/.infoflow_config`. It mints and refreshes its own app access token; no token is ever exported into a shell or handled by the control plane. Default gateway URL is `http://127.0.0.1:18791` (`TOM_AUTODEV_INFOFLOW_GATEWAY_URL`); the journal defaults to `~/.tom-autodev/infoflow-replies.jsonl` and a gateway journaling elsewhere fails the send with `INFOFLOW_GATEWAY_JOURNAL_MISMATCH`.

The gateway only journals messages. Every decision is authorized by `InfoflowReplyConsumer` against the stored request, so a reply is applied only when it:

- quotes the `approval_id` or `request_id` (`approval_id` is unique per `run_id`/action/`input_hash`, so quoting it binds the answer to that content hash);
- says exactly one of `APPROVE` or `REJECT`;
- comes from a sender whose uuap resolves to exactly one member of `member_policy["infoflow"]`;
- was typed either in a private chat or in the run's own collaboration group — a group reply carries the `group_id` the gateway journalled, and the same words in another group are not a decision about this run;
- arrives after the request was created and no later than its deadline.

A group message from a robot carries no `fromuserid`, only a numeric robot id that resolves to nobody, so the gateway does not journal it as a sender at all.

The applied decision is persisted under its own idempotency key, so the same reply can never resolve a gate twice and a later `await-approval` sees the stored answer instead of re-reading the journal. When the gateway cannot run at all, `tom-autodev approve <approval_id> APPROVE <input_hash> infoflow <run_id> <responder>` remains the fallback.

## Landing a decision without the CLI

`await-approval` only reads the journal while a command runs, so a reply typed into 如流 would otherwise wait for the next invocation. `tom-autodev watch-approvals [--interval N] [--once]` polls the open approvals, lets the transport apply any bound reply, and answers the approvers in 如流 once per approval (`approval.ack:<approval_id>:<decision>`), including a timeout notice. It never advances a phase: phase work needs the agent, so a background process must not decide engineering on its own.

Each round also protects the inbound path, because a reply only exists while the WebSocket is connected and 如流 offers no way to fetch missed messages:

- the gateway is health-checked and revived, bounding a lossy window to one interval;
- an undecided gate is reminded at roughly six, two, and half an hour before its deadline, repeating the exact card that was delivered (`approval.nudge:<approval_id>:<bucket>`);
- a gate that times out anyway is reissued once as `<action>#retry-<n>` against the same `input_hash` (`approval.reissue:<approval_id>`), because `TIMEOUT` is terminal in the ledger and the approvals row is unique per `(run_id, action, input_hash)`. `tom-autodev reissue-approval <run_id> <action> <input_hash>` does the same by hand and refuses while the gate is still open.

A failed reminder or acknowledgement is reported (`APPROVAL_REMINDER_FAILED`) but never raised: the decision is already in the ledger, and a watcher that died on a courtesy message would leave every later reply unread.

Neither the gateway nor the watcher is a system service. The gateway is spawned detached and survives the CLI, the watcher lives with the shell that started it, and both stop at reboot. While no watcher runs, nothing revives the gateway and nothing reminds or reissues, so replies sent in that window can still be lost; the fallback stays `tom-autodev approve <approval_id> APPROVE <input_hash> infoflow <run_id> <responder>`.

## Knowing where a run stands

`tom-autodev status <run_id>` prints the position rather than the event log (`--json` still dumps it): current phase, when it last changed, open gates with their deadlines, external writes that are not confirmed yet, the blocking reason if any, and one line saying who acts next and what they do. `run_brief.build` derives all of it from `next()`, the approvals ledger, and pending intents, so it cannot disagree with what the control plane will actually allow.

The watcher pushes the same lines to 如流 on every state change, so a phase landing does not stay invisible until someone opens the CLI:

- one notice per event, keyed `progress-notice:<run_id>:<event_id>`;
- a run seen for the first time is recorded silently, so a restarted watcher never replays old positions;
- a notice is suppressed while a gate is open, because the gate card already says what to do;
- it goes to the run owner from `collaboration_binding`, not the whole role list, so the channel carrying approvals stays worth reading.

A gate being approved changes no state, and landing the phase needs the agent, which only runs when someone types 继续 in the IDE. Between those two moments a run looks alive while it is in fact waiting, and the progress notice cannot report it (no state change, and it stays quiet while a gate is open). `ApprovalWatcher._handoff` covers exactly that gap:

- it fires when nothing is open, nothing is in flight, nothing is blocked, the next actor is Comate, and the gate the current phase requires (`run_brief.build`'s `gate`) carries an APPROVE that landed after the current state event;
- it goes to the run's collaboration group, because that is where the approver just replied;
- it is keyed `resume-notice:<run_id>:<event_id>:<approval_id>`, and matching on the *current* phase's gate is what stops it repeating after the phase lands: the next phase asks for a gate that has no decision yet. The `resolved_at` comparison covers the other direction — a phase that is re-entered (see `WORKSPACE -> TASKS` in `references/state-machine.md`) discards the decision that approved it, and that stale APPROVE must not read as "your turn";
- a failed send is reported as `RESUME_NOTICE_FAILED` and never raised.

## Approval card

`_approval_markdown` renders the gate. Two rules come from how Infoflow parses Markdown:

- a link label containing whitespace is rendered twice, so labels are emitted whitespace-free (`_link_label` hyphenates);
- line breaks travel as real newlines, because the gateway sends JSON rather than shell arguments.

Requirement documents come from the profile's `provider: ku` knowledge sources in `priority` order; a source may carry `label` for a short human title, otherwise `search_scope` is used.

Every card also states what the gate decides (`**本次审批**`), what approving causes (`**批准后**`), and a summary of the artifact (`**内容摘要**`). Subject and effect come from `approval_summary._GATES`; the summary is derived from the artifact itself, never from prose written by hand:

```bash
python3 scripts/cli.py request-approval RUN_ID G1 INPUT_HASH --content /path/to/artifact-envelope.json
```

`--content` takes the phase envelope (or a bare `content` object). The content is canonicalized and hashed here, and when the file is an envelope its `content_hash` and `approval_input_hash` must agree with what is being requested, otherwise the gate is refused with `APPROVAL_CONTENT_MISMATCH` rather than delivering a summary of something else. Without `--content` the card omits the summary, except `G0`, which summarizes the collaboration binding and card hash from the run's own start event.

Next to the markdown card the gate also gets a one-tap button card, so the decision does not require retyping a 32-hex id on a phone. The buttons carry the decision in their own `eventKey` (`approve.<approval_id>` / `reject.<approval_id>`), a tap arrives over the same WebSocket as `event.interaction_submit`, and the gateway journals it as the exact `APPROVE <approval_id>` a person would have typed — so the authorization rules above apply unchanged. The card is only sent when the run has a collaboration group, and a card that fails to render is recorded in the stored receipt without touching the gate: the markdown message, which carries the links, hashes and documents, stays the record.

## Asking a non-gate decision

A run also stops at judgement calls that are not gates — which repair to take first, whether to continue into the next phase. Asking those only in the IDE leaves them invisible until someone comes back to look, so ask them in 如流 too:

```bash
python3 scripts/ask_infoflow.py ask RUN_ID --title "tom-autodev 需要一个决定" \
  --question "..." --line "..." --option "continue=继续推 PLAN" --option "hold=先停一下"
python3 scripts/ask_infoflow.py wait RUN_ID DECISION_ID [--timeout 900]
```

`ask` sends a button card to the run's collaboration group and records the question under `~/.tom-autodev/decisions/<decision_id>.json`; `wait` polls the journal and returns the tapped option. An answer counts only when it was tapped in that group after the question was asked and names one of the options that were offered.

This is deliberately weaker than a gate and must never replace one: there is no ledger row, no bound `input_hash`, no deadline and no member policy behind it. Ask a real gate through `request-approval`. A `TIMEOUT` from `wait` is not a decision — fall back to asking in the IDE.



