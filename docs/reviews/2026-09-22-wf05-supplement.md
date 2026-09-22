# WF-05 补充证据：真实 worker 并发与锁边界

本记录补充 [初审 B](2026-09-22-initial-b.md) 的 `GAP-WF05`，不改写初审结论，不代表 C/M/P 整体验收。

- 标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`。
- 仓库/分支：`/Users/tom/Desktop/skills/tom-autodev-suite` / `phase1-workflowspec`。
- 原始 target_commit：`273d2507bcfb600d1a1ab9e5e6d8f61b72d41565`；原始只读快照：`/tmp/tad-review-273d250-elgq4r02`。
- 补证执行者：Codex 独立上下文 B；具体模型版本未知；日期：2026-09-22。
- 范围：同 run 竞争、两个不同 run 共享源仓、租约过期接管、重放、显式注入锁及 `locks=None` 边界；不执行真实平台调用、业务编译或业务测试。
- 环境：macOS 26.6.2 arm64 / Python 3.9.6；真实临时 Git、SQLite、`Orchestrator`、`worker_driver.advance`、`WorkspaceManager` 与 `LockManager`；仅外部平台/知识边界使用 fixture。
- 新证据文件：[test_worker_concurrency_contract.py](../../tom-autodev/scripts/tests/test_worker_concurrency_contract.py)，SHA-256：`507472c6f86c014fa26f9e61fc8f72719e5b81f45ec4b5f7ecb5a0ab4c245715`。没有改生产代码。

## 用例与实际不变量

| 用例 | 实际调用与断言 | 结果 |
|---|---|---|
| `test_two_runs_share_source_repositories_without_crossing_workspace_ownership` | 两个真实控制器共享 config/SQLite/worktree root，同时调用 worker；两个 run 共享业务与独立测试源仓。在真实 `_ownership_operation_lock` 外加入 barrier，确认同一资源锁两方都进入竞争、锁内最大并发为 1。两方停在各自 G4，worktree path/token 不交叉；审批后并发续跑到各自 PLAN，重放保持 job/events/所有权。源仓 HEAD、branch、status、已跟踪和未跟踪用户内容不变；每 run 三次 acquire/release，最终无活跃租约。 | 通过 |
| `test_same_run_worker_lease_blocks_a_second_real_worker_before_workspace_effects` | 首个 worker 在 workspace 创建前暂停；第二个控制器驱动同 run 返回 `WORKER_LEASE_HELD`，不写事件或所有权；首个随后完成并释放。 | 通过 |
| `test_expired_dead_worker_lease_is_taken_over_and_replay_keeps_owned_worktrees` | 设置 dead PID/已过 TTL 租约，真实 worker 接管；重放保留 worktree 和 gate hash，历史为 acquire/takeover/release/acquire/release。 | 通过 |
| `test_locks_none_is_single_process_mode_and_does_not_consult_the_run_lease` | 已存在 run 租约时，顺序执行的 `locks=None` 不查询该租约并能继续，重放稳定且既有租约不变；随后显式带锁驱动正确阻断。该测试只界定单进程调用语义。 | 通过；不证明无锁并发安全 |

## 执行记录

工作区测试命令：

```text
$ PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tom-autodev/scripts/tests -p 'test_worker_concurrency_contract.py' -v
test_expired_dead_worker_lease_is_taken_over_and_replay_keeps_owned_worktrees ... ok
test_locks_none_is_single_process_mode_and_does_not_consult_the_run_lease ... ok
test_same_run_worker_lease_blocks_a_second_real_worker_before_workspace_effects ... ok
test_two_runs_share_source_repositories_without_crossing_workspace_ownership ... ok
----------------------------------------------------------------------
Ran 4 tests in 4.510s

OK
```

随后把原始快照的 `scripts/tests` 和 `scripts` 放在 `sys.path` 首部，通过 `importlib.util.spec_from_file_location` 仅加载同一个新增测试文件。打印并核对 `worker_driver`、`workspace_manager`、`orchestrator`、`lock_manager` 的 `__file__` 均来自原始快照，得到：

```text
----------------------------------------------------------------------
Ran 4 tests in 4.549s

OK
```

原始生产代码同样满足这四项；这是补齐已有行为证据，不是修复用例。根任务后来报告完整控制面回归收集了这四项，合计 961 tests / 116.666s / OK；完整套件数字不是上述不变量的替代证明。

## 宿主与范围边界

已核对 [Worker 接入](../OPERATIONS.md#worker-接入) 和 `worker_driver.advance`：宿主负责装配并发驱动所需 `LockManager`；`locks=None` 明确不启用 run 租约。本仓提供 Python worker 接口和注入合同，没有一个可直接启动的独立 Comate 服务。因此此处证明的是合同规定的实际 API 路径和资源锁，而不是声称已核验外部宿主部署。

本地单机所支持的 C 路径中，初审缺少的跨 run 源仓竞争、同 run 排他、租约接管和重放现已补证。真实 Comate 宿主锁装配仍未进行现场集成验证；跨主机/共享文件系统的锁语义也未测试，不能据此推导 P 资格或扩展支持环境。

没有为未验证部署作风险接受。最终 C 结论仍需合并其他 WF 项、重大缺陷处置及最终 commit 身份；本项补证到此停止。
