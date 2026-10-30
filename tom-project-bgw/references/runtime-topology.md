# BGW Runtime Topology and iPipe Parameters

This file is project knowledge for `tom-autodev`. It records the BGW test topologies
for its four iPipe pipelines (100G/25G × bgw/bgwagent),
where to read logs, how to inspect versions and counters, and which iPipe stage
parameters belong to which compiled product. It does not execute iPipe, iCode, or
remote writes. Match the current profile and runner evidence before using recorded
hosts, paths, versions or pipeline IDs; a historical address is not a target binding.
The parent controller fills parameters, requests G8, and reruns stages.

## Test Topology

BGW/bgwagent has four stable iPipe pipelines: 100G bgw, 100G bgwagent, 25G bgw, 25G
bgwagent. In every one the **client and RS are the same host** — `sep6`
(`bjkjy-sys-ip-base-sep6.bjkjy`, client `xgbe0: 192.168.37.8`, RS `xgbe1: 192.168.38.8`)
— and traffic reaches BGW over the VIP/vs. Only the **BGW server host + binary path** and
the **qa branch** change per pipeline and test type. These addresses are reference
knowledge, not a target binding: match the current profile and runner evidence before
connecting, and update this file if the real config differs.

| Pipeline | BGW server host | BGW binary |
|---|---|---|
| 25G bgw | `bjkjy-sys-ip-base-sep5.bjkjy` (control plane `10.130.21.19:6666`) | `/home/work/x86bgw_for_agile/output/bgw` |
| 25G bgwagent | `bjkjy-sys-ip-base-sep7.bjkjy` | `/home/work/x86bgw_for_cov/output/bgw` |
| 100G bgw | `bjkjy-sys-100g-x86bgw-dev01.bjkjy` | `/home/liuman/x86bgw_for_agile/output/bgw` |
| 100G bgwagent | `bjkjy-sys-100g-x86bgw-dev01.bjkjy` | `/home/liuman/x86bgw_for_agile/output/bgw` |

Test type sets the qa branch on both client and RS (the client always also carries
`bgwagent`):

- `P0级自动化回归测试` → qa **master** branch.
- `P0新case回归测试` → qa **新用例 (new-case)** branch.

Only the bgw pipelines document a `P0新case回归测试` variant; the bgwagent pipelines run the
master-branch `P0级自动化回归测试`.

For the 25G bgw pipeline, cases talk to BGW through `bgw_auto/tool/bgwagent -b 10.130.21.19`;
other pipelines use their own server control-plane address from the profile. Do not infer
any of this from a directory name or from a Mac checkout.

## Who Replaces What

- Earlier x86bgw compile/regression stages replace the **server** BGW binary on `sep5`.
- `P0新case回归` replaces **client** artifacts on `sep6` only:
  - `get_bgw_test_case` updates `/home/work/yueyufei/x86bgw/bgw_auto`
  - `get_bgwagent` updates `/home/work/yueyufei/x86bgw/bgw_auto/tool/bgwagent`
- This stage must not restart or overwrite `/home/work/x86bgw_for_agile/output/bgw`.

A QA tarball ships an old `tool/bgwagent` (`bgwagent-stable-21.09-1`, magic `0x20210705`).
The current BGW expects `D_BGW_MSG_MAGIC_NUM = 0x20220228`. Using the tarball tool against
the already-updated server produces `Wrong message magic number: 20210705` and RST.

## Associated Logs

Read these in this order after a product-case failure:

1. iPipe job log from the failed stage. It shows download, `fetch_cr.sh`, `success_ratio`,
   and which case directories ran.
2. Per-directory `out` and `res.csv` on the client:
   `/home/work/yueyufei/x86bgw/bgw_auto/<case_dir>/out`
   `/home/work/yueyufei/x86bgw/bgw_auto/<case_dir>/res.csv`
3. BGW syslog on the server:
   `/var/log/messages` tagged `x86bgw`
4. Optional BGW session log:
   `/var/log/bgw-session.log`

`fetch_cr.sh` creates `/home/work/yueyufei/x86bgw/bgw_auto/log` before running cases.
Missing that directory fails in `setup_logging()` with no `res.csv`. That is a runner
precondition, not a product assertion.

## Version Inspection

Confirm the two ends of the control-plane conversation before blaming a case:

```text
client tool
/home/work/yueyufei/x86bgw/bgw_auto/tool/bgwagent -h
# first line is the agent version / commit

server binary
/home/work/x86bgw_for_agile/output/bgw
pid from: ps -e -o pid= -o args= | grep '[.]/bgw'
listen: ss -lntp | grep 6666
```

The parent may use [tom-diagnose's bundled remote mode](../../tom-diagnose/references/remote-operations.md)
for these checks against its authorized target. Confirm the current profile still
uses this topology before connecting. Do not replace the server binary while
diagnosing `P0新case回归`.

## Counter and Stats Inspection

After a case actually talks to BGW, read counters through the same `bgwagent`:

```text
bgwagent -b 10.130.21.19 --list-vs
bgwagent -b 10.130.21.19 --list-sg-stats
bgwagent -b 10.130.21.19 --list-kpd-stats
```

Offer-hash cases compare NAT44 `get_stats_counter` deltas. If `--unlock` or `--list-vs`
already RST, counters are not evidence of product behavior.

## iPipe Parameter Mapping

`P0新case回归` is on x86bgw `ChangePipeline(348102)`. Its manualParams may be empty
despite static job/upstream dependencies. Discover declarations and inputs before G8;
the current HTTP adapter blocks exact job override pending a verified platform contract.
Use the run's frozen changed status, not worktree clean, to select products:

| Parameter | Module | Product | Consumed as |
|---|---|---|---|
| `get_bgw_test_case` | `baidu/nsiqa/x86bgw` | QA ChangePipeline `504074` compile `productHttpUrl` | wget into `bgw_auto` |
| `get_bgw` | `baidu/sysip/x86bgw` | Changed: owned exact candidate x86bgw ChangePipeline `348102` compile `productHttpUrl` | bgwagent `348142` `p0级自动化回归测试`: wget, untar, start the BGW under test |
| `get_bgwagent` | `baidu/sysip/bgwagent` | Changed: owned exact candidate ChangePipeline `348142` compile; unchanged: target branch BranchPipeline `348141` successful releaseOutputHttpUrl | wget, then copy `output/bgwagent` over `bgw_auto/tool/bgwagent` |

When `P0新case回归` runs after `p0级自动化回归测试` in the same build, it inherits
`get_bgwagent` from that upstream stage: the control plane fills it there under G8 and
then fills only `get_bgw_test_case` for `P0新case回归` (this run's QA ChangePipeline
`504074` product). An upstream-triggered run that this control plane did not fill uses the
configuration's hard-coded default product, which goes stale (BGW-2019: `nodes/102205340`
answered 404); rerun the stage through G8 instead of treating that as a test result.

Both inputs obey the frozen module policy. Verify branch/source revision/job identity
and actual download. An unchanged module without a published product may only use an
approved adapter-proven compile-only BranchPipeline operation; the current API cannot
prove release exclusion and blocks. Never use arbitrary historical compile/latest or
node97110098 as fallback. See the parent's phase protocol for read-only measured API evidence.

Do not map `get_bgw_test_case` by basename `x86bgw`; that collides with `baidu/sysip/x86bgw`.
Do not treat a filled `get_bgwagent` as proof the job replaced the tool; the job script must
consume it after updating `bgw_auto`. Tokens stay in `~/.tom-autodev/credentials/irepo-tokens.yaml`
and are redacted in evidence.

### Release path (BranchPipeline `348101`)

After the CR merges, iCode starts x86bgw **BranchPipeline `348101`** on the target branch for
the merged revision. That pipeline, not ChangePipeline `348102`, performs the release. Its
stages, in order: 编译 → 扫描 → **自动化回归测试** (MANUAL) → 系统测试功能回归 (MANUAL) →
**提测** (MANUAL, `APP_TEST_REQUEST`) → 准出审核 → 测试准出 (QA confirms on the platform) →
**发布** (MANUAL, `AGILE_RELEASE`).

| Stage | Declared input | Filled with |
|---|---|---|
| 自动化回归测试 (stageConf `5002481`) | `get_bgwagent` (stage `paramBeans`/`inParamsMap`; `required: false`, default `" "`) | Same rule as above. The job runs `cd tool; eval ${get_bgwagent}; tar -zxvf output.tar.gz`, so an empty value breaks every case even though the platform says "not required" |
| 提测 | none in stage config; the job carries card, iTest plan, QA owner, module/commit | Platform-derived from the merged CR and card |
| 发布 | none | — |

Measured on 2026-10-27 (BGW-1995): BGW-1918's released build 273787582 filled
`get_bgwagent` with a bgwagent `nodes/<id>` compile product because bgwagent changed in that
requirement; an unchanged bgwagent uses its BranchPipeline `releases/<version>` product.
The release evidence is the 发布 job's own detail (`version`, `revision`, `branch`,
`releaseOutputHttpUrl`); `/api/agile/getReleaseInfo` answers 404 on this gateway.

**Reading declarations — lessons from BGW-1995.** A stage's inputs are declared in the
pipeline *configuration* (`stages[].paramBeans` / `inParamsMap`, and job configuration),
not in a build's `stageBuild.manualParams`, which is `null` for every stage of 348101
including the one that requires `get_bgwagent`. Never report a stage as parameterless
from `manualParams`, a stage detail, or one build's run record; read the live pipeline
configuration (`get_pipeline_by_id(..., brief=False)`), and confirm against a successfully
released build's job log of the same pipeline when unsure. `required: false` is not proof
that a value may be empty — read how the job consumes it.

## Parent Scheduling Contract

Return this knowledge to `tom-autodev`. The parent:

1. Discovers stage/job declarations and upstream inputs even with empty manualParams.
2. Resolves verified products using the frozen changed/branch/revision policy.
3. Fills `get_bgw_test_case` and `get_bgwagent`.
4. Requests exact G8 only if runtime override is supported; otherwise returns blocking.
   No pipeline template changes. Unknown write results only reconcile persisted intent.
5. Reads the new job log, then client `out`/`res.csv`, then `/var/log/messages`.
6. Classifies `ENV_UNSATISFIED` versus product `TEST_FAILURE` before repair.

This skill never calls iPipe, never reruns a stage, and never writes a BGW binary.
