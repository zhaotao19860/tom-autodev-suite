# BGW Runtime Topology and iPipe Parameters

This file is project knowledge for `tom-autodev`. It names the live BGW test topology,
where to read logs, how to inspect versions and counters, and which iPipe stage
parameters belong to which compiled product. It does not execute iPipe, iCode, or
remote writes. The parent controller fills parameters, requests G8, and reruns stages.

## Test Topology

The 25G product-case topology used by `P0新case回归` is:

```text
client
bjkjy-sys-ip-base-sep6.bjkjy
xgbe0: 192.168.37.8
        │
        │  VIP / vs traffic
        ▼
bgw
bjkjy-sys-ip-base-sep5.bjkjy
eth1: 10.130.21.19
control plane: 10.130.21.19:6666
binary: /home/work/x86bgw_for_agile/output/bgw
        │
        │
rs
bjkjy-sys-ip-base-sep6.bjkjy
xgbe1: 192.168.38.8
```

`sep6` is both client and RS. Cases talk to BGW through `bgw_auto/tool/bgwagent -b 10.130.21.19`.
Do not infer this topology from a directory name or from a Mac checkout.

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

The parent may use `tom-autodebug` for these read-only checks. Do not replace the
server binary while diagnosing `P0新case回归`.

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

`P0新case回归` is a manual stage on x86bgw `ChangePipeline(348102)`. The parent fills
these names from this run's compiled products:

| Parameter | Module | Product | Consumed as |
|---|---|---|---|
| `get_bgw_test_case` | `baidu/nsiqa/x86bgw` | QA ChangePipeline `504074` compile `productHttpUrl` | wget into `bgw_auto` |
| `get_bgwagent` | `baidu/sysip/bgwagent` | bgwagent ChangePipeline `348142` compile `productHttpUrl` | wget, then copy `output/bgwagent` over `bgw_auto/tool/bgwagent` |

Do not map `get_bgw_test_case` by basename `x86bgw`; that collides with `baidu/sysip/x86bgw`.
Do not treat a filled `get_bgwagent` as proof the job replaced the tool; the job script must
consume it after updating `bgw_auto`. Tokens stay in `~/.tom-autodev/credentials/irepo-tokens.yaml`
and are redacted in evidence.

## Parent Scheduling Contract

Return this knowledge to `tom-autodev`. The parent:

1. Discovers the failed/manual stage and its parameter names.
2. Resolves product URLs from this run's current revisions.
3. Fills `get_bgw_test_case` and `get_bgwagent`.
4. Requests G8 and calls `ipipe-rerun`.
5. Reads the new job log, then client `out`/`res.csv`, then `/var/log/messages`.
6. Classifies `ENV_UNSATISFIED` versus product `TEST_FAILURE` before repair.

This skill never calls iPipe, never reruns a stage, and never writes a BGW binary.
