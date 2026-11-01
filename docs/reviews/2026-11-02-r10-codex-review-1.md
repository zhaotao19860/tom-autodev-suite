# 局部变更复核

- 标准：`TAD-REVIEW-EXIT / 1.0`；SHA-256 已核对：`5937af637dbaf681f0c3046a1a3643de2dd0a3be6ee55ae9b4421aacff9e0393`
- 分支：`mvp`
- base：`a4733d118441c65b7e2b9774ed910a6bcc9f1697`
- target：`eb9ed7c981cff557e3098963459d1d354b61c085`
- 范围：指定差分及其调用方，仅局部 C 复核。

## Findings

### REC-01 · P2：更换 profile 后，已有 build 的 run 无法通过 CLI 重开承接

**位置：** `tom-autodev/scripts/orchestrator.py:1823、4921–4923、4985–4987`；`tom-autodev/scripts/restart.py:242–245`  
**违反合同：** WF-04、WF-08；`docs/OPERATIONS.md:232` 承诺的 profile 变更恢复路径。

**失败场景：** run 已记录一个结束的 build，没有未决 intent、待处置 CR 或脏工作树；将同路径 profile 的 `submission_policy` 从合法配置 H0 改为 H1，再执行 `restart-from`。

实际调用链：

```text
_restart_build_status → _cli_ipipe_runtime → _runtime_profile
→ PROFILE_CONFLICT → 返回恒为 None 的 build 查询
→ RESTART_SOURCE_UNRESOLVED
```

平台状态查询根本不会发生，已结束的 build 被当作状态未知。先 `stop` 也不能解除阻断；恢复 H0 后直接重开，目标又只能钉住 H0，不能采用 H1。

**证据及反证：** 内存 SQLite 复现得到 `status=None、GET=0`，停止后仍阻断。使用已有不可变 build binding 构造只读 `build_status` 回调，已验证可以 `RESTARTED`，且目标钉住 H1、源 run 保留 H0，因此属于有绕行的 P2，而非不可恢复卡死。

**建议修法：** restart 的只读 build 处置查询应直接依据持久化 binding 中的 module/revision/pipeline 身份，不依赖源 run 的执行型 profile 校验；普通执行入口继续保留 profile 守卫。补充 H1＋已结束 build 的 plan/execute 回归，并确保运行中 build 仍被拒绝。

---

### FIX46-001 · P2：无关的已放弃尝试会破坏同一 G8 的本地错误重发

**位置：** `tom-autodev/scripts/clients/ipipe_runtime.py:1023–1031`  
**违反合同：** WF-04；`README.md:541` 承诺的“有本地未发送证明时，同审批可重发一次”。

**失败场景：**

1. 同一 stage 在配置 B 下重跑失败，被普通 `abandon-intent` 放弃。
2. 流水线配置改为 A，例如修改 shell 命令，申请新的 G8。
3. A 在调用点发生本地错误，运行时记录 `local_error_proven=true`。
4. 修复调用错误，保持 A 不变，使用 A 的原 G8 重发。

`prior` 包含已放弃的 B、排除本地失败的 A，但随后 `any(...)` 扫描的是**全部** intent，命中 A 后将无关 B 注入 `prior_attempts`，使 A 的 hash 改变。结果不是承诺的一次重发，而是 `APPROVAL_INPUT_MISMATCH`。

**证据及反证：** 同一内存场景对照：

```text
46ca687 父版本：hash_changed=False，原 G8 重发=OK
target：       hash_changed=True，原 G8 重发=APPROVAL_INPUT_MISMATCH
```

重新申请新 G8 已验证可成功，故为 P2。

**建议修法：** 判断“当前绑定是否已消费”时也只扫描 `_consumed` 尝试；不能让仅有本地未发送失败的 A 触发其他绑定历史进入 hash。增加“普通放弃 B → 配置 A → 本地错误 → 原 G8 重发”的回归。

---

### R9-NEW-001 · P2，复核未关闭：v4 兼容修复未保留已缓存的 INTAKE action 身份

**位置：** `tom-autodev/scripts/collaboration.py:170–177`；关联 `phase_protocol.py:276、306、335–361`  
**违反合同：** WF-04、WF-08；`2db1e44` 对历史 v4 INTAKE 的兼容承诺。

**失败场景：** `3ff3ee1` 创建了 v4 scope run，并已调用 `next()` 缓存 INTAKE action。当时 prerequisite 字段集包含 `requirement_diff: null`。升级后，新 matcher 接受历史 G0 hash，但 `intake_prerequisites()` 仍按当前 V4 字段集返回，不再包含该键。

因此 G0 hash 虽相同，重算的 `action_id` 却不同，而缓存键不变；`next()`、只读 `next()` 和未完成的 `complete()` 均返回 `ACTION_CONFLICT`，无法使用原 G0 继续。

**证据及反证：** 用真实历史代码复现：历史 `next=OK`，target prerequisite 校验通过，新旧 action 仅 `action_id` 不同，随后命中上述冲突。新增测试只验证 matcher，没有覆盖已缓存 action。干净 INTAKE 通过 `restart-from` 重开并继续的绕行已验证，故为 P2。

**建议修法：** 根据持久化 `g0_input_hash` 精确识别历史 v4 使用的字段集，并让校验与 action 身份重算一致使用它；不要删除缓存或放宽任意 hash。补充“历史 v4＋已缓存 action＋原 G0 继续”的回归。

---

### REL-001 · P2：删除 `release_info` 后，fixture 录制入口仍调用该方法

**位置：** `tom-autodev/scripts/tests/fixtures/platform/record.py:134–135`  
**违反合同：** WF-10 相关证据采集链；MVP 第 6 节的平台 fixture 合同。

**失败场景：** 执行该文件声明的录制命令，前 11 项成功后，最后一项仍调用 `api.release_info(...)`。真实 `IpipeApiClient` 已删除该方法，立即抛出 `AttributeError`；`error()` 只捕获 `IpipeTransportError`，命令因此在已经更新前面 fixture 后失败。

**证据及反证：** 只读加载 recorder，并对真实客户端执行最后一个 TARGET，已复现：

```text
AttributeError: 'IpipeApiClient' object has no attribute 'release_info'
```

现有回放测试使用自行实现此方法的 `ReplayApi`，不能发现 recorder 与真实客户端不兼容。已有 fixture 仍可回放，所以这是录制工具的局部回归，不是生产发布阻断。

**建议修法：** 从录制目标中移除旧端点、保留历史 404 fixture；若仍需采集该端点，则在 recorder 内显式使用只读 transport 请求，不恢复生产 fallback。增加 TARGETS 与真实客户端的兼容检查。

## 结论

**FAIL（仅本轮局部 C 差分复核）**：上述 4 项 P2 均未修复，亦未见负责人明确接受记录。

取证包括 **57 项选定 unittest 通过**、只读/内存复现及父版本对照。base/target 的 workflow canonical hash 均为 `76bb964c9bb4f513366450cb11ed569129672011f635201b44d570734730d410`，本次删除未改变该 hash。未修改文件，未运行业务项目编译或测试；全量 C、M、P 不据此判定通过。