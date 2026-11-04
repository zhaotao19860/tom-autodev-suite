# BGW 领域知识参考

本文件属于 `tom-diagnose`（2026-11-04 由 tom-autodebug 的 `bgw-domain-knowledge.md` 与 README 中的 BGW 诊断说明合并而来）。定位 BGW 问题时优先读取这里，再按问题读取下列手册。本文件是方法与易误判规则；具体环境事实（主机、拓扑、部署路径）以 bgw-workspace `docs/environment/` 及 action 提供的 `knowledge_workspace` 为准。覆盖数据侧的 `newttm`、`x86bgw`、`bgwagent` 三个子系统和控制面的 `controller`。`bgwagent` 是配置/查询工具，不负责逐包转发。

资料核实日期：2026-10-19。下列 7 篇使用/配置/编译手册及内搜接口文档均已成功读取；源码索引是用户指定入口，不表示每个远端分支均已逐项审计。源码摘录另注明实际核实基线。每次诊断仍应匹配运行版本，不能把本文件当作所有版本的完整规格。

## 0. 诊断顺序

涉及 `newttm`、`x86bgw`、`bgwagent` 或 controller/ACC 时，按以下顺序取证：

1. 读取本文件，匹配现场二进制、agent、内核模块和 controller 版本（第 2 节）。
2. 记录运行版本、启动命令、实际配置、VS/RS 状态和统计采样区间。累计计数只描述其采集范围内的历史，必须用同一实例、同一查询口径的前后两次采样增量解释，不能代替当前状态。
3. 分开核对 RS `vld`、健康检查来源、CDN Offer Hash flag 和 TTM。`ecv_enable=1` 表示外部提供 RS 状态，`ecv_enable=0` 才是 BGW 本地 checker 路径（第 4 节）。
4. 配置不一致时沿 controller 期望配置 → 生成文件 → bgwagent 加载 → x86bgw 运行态逐层比较；不能把 shell 历史、示例文件或未发现 controller 当作最终配置来源。
5. 只有在响应、解析、聚合、worker 同步和数据面都具有匹配版本证据后，才把“RS 被 Offer Hash 屏蔽”扩展为“响应没有携带允许 flag”。

远端现场（经 [remote-operations.md](../remote-operations.md) 的 relay/tmux 只读取证）先看 `ps -ef | grep bgw`、实际加载的 `bgw.conf`、`env_start`、接口信息、DPDK 端口和相关日志。不要把手册中的路径、示例配置或历史测试环境当作当前现场；VIP 不通的通用分角色模板见 [remote-troubleshooting.md](../remote-troubleshooting.md)。

## 1. 子系统和证据边界

| 子系统 | 职责与优先取证点 | 不能直接推出的结论 |
|---|---|---|
| newttm | RS 侧内核模块，接收/处理 BGW 透传的 CIP/VIP 等 TTM 元数据；检查内核、模块版本、启用项及模块统计 | 模块已加载不等于 BGW 已加 TTM，也不等于应用已成功取得 CIP/VIP |
| x86bgw | BGW 转发程序；检查 worker 数据面、NAT/调度/session、KPD 健康状态、网口/DPDK 和运行配置 | Linux 内核网络可达不等于 DPDK 数据面正常；RS 健康不等于所有调度策略都允许选它 |
| bgwagent | 用户与 BGW 之间的配置、查询、调试命令接口；核对 agent 版本、目标地址、参数解析、请求/返回结构及显示逻辑 | agent 没显示某字段不等于 BGW 没保存；CLI 修改成功不等于 controller 权威配置也已更新 |
| controller（ACC） | 控制面生成 VS/RS 业务配置，调用 bgwagent 加载到 BGW；检查期望配置、生成文件、加载结果及更新时间 | 控制面期望值不等于运行生效值；不能假定它就是 RS 外部健康状态的唯一提供方 |

业务配置链路由配置手册确认：`controller 生成业务配置 → bgwagent 加载 → x86bgw 生效`。TTM 问题沿 `x86bgw 加元数据 → 转发报文 → RS newttm → 应用取值` 分段取证。健康状态来源须单独确认，不能仅按进程名推测。

controller 的代码库已确认是 [sysnoc/acc master](https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysnoc/acc/tree/master)。先按现场版本和部署组件匹配代码，再用内搜和现场进程/日志确认实际控制面链路；不要把 ACC master 当前代码自动等同于线上部署版本，也不要从 x86bgw 仓库路径拼出 controller 仓库或 API。

## 2. 代码入口与版本选择

| 子系统/分支 | 代码链接 |
|---|---|
| x86bgw / x86bgw-dpdk-2011 | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysip/x86bgw/tree/x86bgw-dpdk-2011 |
| x86bgw / x86bgw-dpdk-1711 | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysip/x86bgw/tree/x86bgw-dpdk-1711 |
| bgwagent / x86bgw-dpdk-2011 | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysip/bgwagent/tree/x86bgw-dpdk-2011 |
| bgwagent / x86bgw-dpdk-1711 | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysip/bgwagent/tree/x86bgw-dpdk-1711 |
| newttm / master | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sys-ip-x86bgw/newttm/tree/master |
| controller / ACC master | https://console.cloud.baidu-int.com/devops/icode/repos/baidu/sysnoc/acc/tree/master |

先从现场记录可执行文件路径、启动命令、版本/commit、内核、DPDK/网卡驱动及 agent 目标地址。`bgwagent` 使用手册说明 `-b/--bgwaddr` 默认端口为 6666；必须确认实际查询的是目标实例。

按现场选择 1711/2011 或具体补丁分支/commit，分别核实 x86bgw 与 bgwagent 的版本。newttm 同时匹配内核与模块版本。主干/手册仅能作为导航，不能证明某功能已经存在于运行二进制。不要为查看另一版本而切换或覆盖用户当前工作树。

## 3. 手册索引与读取顺序

以下标题已从 KU 正文确认：

| 编号 | 文档与链接 | 优先用于 |
|---|---|---|
| U1 | [bgwagent使用手册](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/y0pviwbxjQqyFG) | CLI、配置/统计字段、ECV、抓包、健康检查 |
| U2 | [newttm使用手册](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/pKzJfZczuc/eQGoAkCA56/_KhMHmE96r98Fv) | CIP/VIP、内核模块版本、开关与统计 |
| C1 | [X86BGW 配置手册](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/4e5affYkdrXRwW) | 主配置、VS/RS、controller 配置链路、BGP/NUMA/BIP |
| B1 | [物理机编译](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/jw9P4PkKV8wy0D) | DPDK/OFED、x86bgw 和 bgwagent 构建依赖 |
| B2 | [docker编译](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/cPLry349ymOiPH) | 容器环境、构建产物；不是运行现场配置 |
| B3 | [云脉版本编译](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/63wq9UIZdOCyh2) | 云脉 XSC/DPDK 构建补充 |
| B4 | [newttm编译](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/pKzJfZczuc/eQGoAkCA56/2yOubVYeE2OHXE) | 内核构建环境、KERNELDIR、模块产物 |
| S1 | [如流搜索开放能力-百度内搜接口文档](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/_SKPgSwp2G/pfs9ugM5RS/2gZoiAC5NsKVtx) | 上述资料不足时发现专题、控制面或历史问题文档 |

通过 `ku-doc-manage` 查询 URL 或末段 doc id，协议选 markdown；先读该技能及对应查询参考，按当前用户身份自动鉴权。不要用公开 WebFetch 抓取内网文档。文档较大时保留来源，定向提取相关章节并查看上下文；返回失败或无权限应标注“未读取”，不能依据标题补写内容。

C1 描述主配置常见于 `/home/work/x86bgw/bin/bgw.conf`，包含 CPU/NUMA、端口/BIP、KPD、session 同步等启动参数，大部分不支持热加载。业务配置常用 `vip.conf`、`bgwtop.conf`，由 controller 生成，线上生成文件常见于 `/ssd1/tmp`，文件名和路径不是协议约束。只读比较文件与运行态；`-F/-X` 是加载操作，不作为只读诊断执行。

C1 给出 zebra/bgpd 配置和日志路径示例。排查 VIP/BIP 路由时结合实际邻居、地址族、prefix-list、route-map 与 NUMA 端口核对，不把示例网段当作当前拓扑。配置手册中的注释也可能有笔误，应结合参数定义和匹配源码确认。

## 4. 健康状态与 ECV

### 4.1 参数的已核实语义

U1 第 5.2.3 节明确写出：`ecv_enable` 为 **1** 时可通过外部指定 RS 状态，BGW 不进行健康检查。第 4.6.9 节也说明 `ecv_chk_num` 是设置了 ECV 的 checker 数，即不发送健康检查报文。

| 字段/维度 | 已核实含义 | 诊断要求 |
|---|---|---|
| `ecv_enable=1` | 外部提供 RS 健康状态，BGW 不主动做该 RS 的常规健康探测 | 查实际状态提供方、下发/接收、超时及回退状态 |
| `ecv_enable=0` | 未开启上述外部 ECV 模式；匹配源码的正常路径由本地 checker 产生健康状态 | 结合 `check_type`、checker、报文和统计判断；不能说“所有健康检查关闭” |
| `check_type` | 本地检查方法 tcp/udp/icmp | 与 ECV 外部状态模式分开，配置存在不等于当前正在发包 |
| `check_data` / `check_result` / `l7_timeout` | 本地七层检查的发送内容、校验内容及响应超时 | 不能把 `ecv_enable` 当成七层开关 |
| `--eh` / `--en` / `--et` | 外部 ECV 当前健康状态、超时后状态及超时 | 修改状态的参数；不在只读排障中执行 `--rsecv-set/-J` |
| RS `vld` / `enable` | 有效状态与管理启用状态 | 不能证明业务负载 flag 为允许，也不能证明响应正文内容 |

源码交叉核实基线：`baidu/sysip/x86bgw`，分支 `x86bgw-dpdk-1711-cdn-url`，commit `cecf198ddfbf7922b0c4f71c04452741c24a3153`：

- `kpd/bgw_check_struct.h`：`CHK_ECV_DISABLE=0`、`CHK_ECV_ENABLE=1`。
- `kpd/bgw_check_state.c`：常规 checker timer 只在 ECV 禁用时启动，注释说明 ECV 状态由 outer world 提供；强制 timer 用于超时/清理等，不代表主动探测。
- `kpd/bgw_check.c`：`kpd_ecv_set_rs()` 接收外部状态、后续状态与超时。
- `ctl/bgw_operation.c`：外部设置 RS ECV 状态要求服务已开启 ECV。

不要再次沿用“`ecv_enable=0` 就是外部模式”这条错误记忆。若其他版本或资料与上述定义冲突，列出冲突并查现场匹配实现，不能静默反转 0/1。

### 4.2 CDN URL 导流的负载 flag

健康状态、CDN 负载 flag 和 TTM 是不同维度。U1 的 ECV 定义不能直接证明 CDN flag 的来源；普通健康检查样例也不构成线上协议。

在上述 **CDN 补丁基线**（不能自动推广到 1711/2011 标准分支）已核实：

- `dataplane/bgw_offer_hash.c` 先排除无效或禁用的 RS，再检查独立的 `offer_hash_flag`。flag 为 0 会被排除出 offer-hash 候选集，不会因此把 RS 改成 down。
- `kpd/bgw_offer_hash_flag.c/.h` 从 UDP 检查响应 **body 最末尾**解析 `#0/#1`；`#1` 允许、`#0` 屏蔽。收到响应但尾缀无效/缺失连续 3 轮后置 0；从未学习到 flag 时默认为 0。未收到响应的 timeout 不推进此 miss 计数，由独立 liveness 处理。响应中间的 `#1` 或末尾还有换行不能当作有效尾缀。
- `kpd/bgw_check_offer_hash.c` 的 `kpd_chk_l7_udp_prefetch()`、`kpd_offer_hash_aggregate()` 分别负责 UDP 响应处理与 checker 聚合；任一参与 checker 的 flag 为 0，聚合即为 0。解析 flag 与响应匹配/liveness 是分开的信号。
- `kpd/bgw_check_state.c` 的 `kpd_offer_hash_flush()`、`kpd_sync_rs_offer_hash_flag()` 将聚合结果下发到 worker RS 副本。排查时需要覆盖响应、解析、聚合、同步、数据面读取各阶段。
- `offer_hash_flag_blocked` 统计被零 flag 排除的候选 RS 次数；`offer_hash_flag_all_blocked` 表示全部健康启用候选被 flag 屏蔽，走普通调度回退。它们不能证明现场已经收到 `#0`，也不能区分“从未学习”“缺失降级”“聚合阻断”或同步问题。
- `ctl/bgw_operation.c` 的 `bgw_fill_rs_ret_info()` 可从 worker 返回 flag/miss；此基线 `GET_RS/GET_SVC_ALL_RS` 与 `GET_ALL_RS(42)` 路径不同，后者不能用留零字段判定真实 flag。先核对查询接口及 agent 的返回/显示代码。

`SGET#1` 是特定测试响应示例，不是所有业务的标准正文。`check_result` 的期望匹配内容与收到的响应尾缀不是同一个配置项；不要直接建议把期望串改成 `SGET#1`，更不能仅为测试而在线切换 ECV 或健康检查协议。

### 4.3 CDN 测试需求与探针编码

2026-10-20 核实的 [BGW-1956 测试需求](https://ku.baidu-int.com/knowledge/HFVrC7hq1Q/9IaD11zSiz/sX0BTOBWJX/DaUGKwCwa761bq) 对上述补丁基线要求 UDP checker、配置 `check_data "SLRTC"`、不配置 `check_result`。任意非空 UDP 响应判活，正文末尾 `#0/#1` 独立控制 Offer Hash；全部候选被屏蔽时回退普通调度，不因此丢包。

注意配置编码与线上载荷的区别：`S` 是字符串类型标记。agent `cfg_parse.c:kpd_data_copy()` 保留该标记；x86bgw `kpd/bgw_check.c` 的配置转换跳过 `check_data + 1`，长度减一；`kpd/bgw_check_proto.c` 的 UDP 构包复制转换后的数据。因此 `--check-data SLRTC` 对应四字节线上载荷 `LRTC`（hex `4c525443`）；`--check-data SSLRTC` 才会发送五字节 `SLRTC`，不能混用。

agent `cmd_parse.c` 的 `SVC_DFT_UDP_CHECKTYPE` 为 ICMP；`-A -u VIP:PORT` 未指定检查方法时会采用 ICMP。只打开 `offer_hash_enable` 不会自动改成 UDP checker。ICMP 路径不进入 `kpd_chk_l7_udp_prefetch()`，未学习的 FLAG 保持 0；此时 `vld` 可为真、miss 可为 0，不能据此推断收到 `#0` 或丢失三轮尾缀。

该基线的 edit 合并逻辑中，显式传 `--check-type udp --check-data SLRTC` 且不传 `--check-result` 会清空旧的 check_result。执行前仍核对版本、完整原配置、授权范围及恢复命令；不能把这个行为推广到其他版本。需求检查间隔与现场值可能不同，应单独确认，不顺手修改。

仅对原始配置为 ICMP、check_data/check_result 均为空且其他字段未变的情形，以下为修复与恢复检查类型的模板（不是授权执行，也不是通用完整回退）：

```bash
./bgwagent --edit-service -u VIP:PORT --check-type udp --check-data SLRTC
./bgwagent --edit-service -u VIP:PORT --check-type icmp
```

第二条通过显式 check_type 且不带 data/result，清空这两个字段并恢复 ICMP；原配置有其他检查参数或控制面配置时，必须按变更前快照逐项恢复权威配置。

使用精确的 service/RS 查询与 `--format 1` 查看 `(oh flag miss)`，例如：

```bash
./bgwagent --list-server -u VIP:PORT -r RS:PORT --format 1
```

先确认查询属于 GET_RS 或 GET_SVC_ALL_RS；GET_ALL_RS 的留零字段不能用于判定真实 FLAG。x86bgw 二进制用 `-v` 查版本，不默认假设支持 `--version`。

主动 UDP 探针必须按配置解码后的字节发送，并限制次数与超时。Linux 管理网源地址/路由与 KPD 的 DPDK/BIP 路径不同：收到 `LRTC#1` 只证明该观察路径响应成立；超时不能直接认定 RS 未实现协议。切换健康检查前需验证所有 RS、所有参与聚合的 KPD 路径，防止将仍可转发的 RS 判为 down。shell 历史能提示手工配置来源，不能证明不存在外部 controller；只读查询未发现控制面时保留这一限制。

现场判断至少记录：VS/RS、运行版本、状态来源、checker/聚合状态、worker 可观测字段、统计采集区间。要判断“响应是否携带 flag”，需要有界响应证据或对应来源链路证据；抓包仍遵守 [remote-operations.md](../remote-operations.md) 的确认与时限要求。已经用匹配实现的统计或运行态证据确认屏蔽、但没有响应证据时，只报告“选路时 flag 被判为屏蔽，具体来源未确认”。若只有填充/显示能力未核实的查询字段零值，连“发生屏蔽”也待确认。累计统计只描述其采集范围内的历史，不能代替当前状态或一次新请求的增量。

### 4.4 BGW-1956 CDN URL Offer Hash 排障清单

按 4.2/4.3 的语义逐项核对，任一项缺证据就停在该项报告缺口：

1. 版本：x86bgw 用 `-v` 查版本（不默认用 `--version`），确认是否为 CDN 补丁基线；agent 版本与查询协议匹配。
2. 检查方法：`-A -u VIP:PORT` 未指定检查方法时默认 ICMP；仅打开 `offer_hash_enable` 不会开始 UDP FLAG 学习，可能同时看到 RS `vld`、`(oh 0 0)` 和全部候选回退普通调度。
3. 配置编码：需求基线为 UDP checker、`check_data "SLRTC"`、不配置 `check_result`；线上载荷是四字节 `LRTC`。手工探针发 `LRTC`，不发五字节 `SLRTC`，也不把 `SGET#1` 当成通用协议。
4. 查询口径：per-RS 状态用 GET_RS/GET_SVC_ALL_RS 路径并带 `--format 1`，例如 `./bgwagent --list-server --udp-service VIP:PORT --real-server RS:PORT --format 1`；`GET_ALL_RS` 的 flag 字段可能按接口约定留零，不能代替 per-RS 查询。
5. 路径：切换到 CDN 测试协议前验证所有 RS 以及所有参与聚合的 KPD/BIP 路径。管理网 UDP 探针只代表该观察路径；超时或收到 `LRTC#1` 都不能替代 DPDK/BIP 证据。
6. 验收：配置变更前保存完整快照（含权威 controller 配置）；变更后用新一轮请求的统计增量验收 `offer_hash_hit`、`offer_hash_flag_blocked` 和 `offer_hash_flag_all_blocked`。

## 5. TTM / newttm 排查

依据 U2，先区分 BGW 服务配置、线上报文、RS 模块和应用调用：

1. 用匹配版本的 bgwagent 查询 `ttm_cip_valid`、`ttm_vip_valid`，结合具体 TCP/UDP、NAT 模式验证加元数据路径。开关为 1 不是成功证据。
2. 在获授权的 RS 上只读查看 `uname -r`、`modinfo newttm`、`/sys/module/newttm/version`、`net.bgw_uinp.*` sysctl 项和 `/proc/net/ttm_all_stats`。不存在的入口应结合版本核实，不能直接判故障。
3. U2 的模块日志示例来自 `/var/log/messages`，需核对当前日志配置。对统计取前后增量，结合流量方向解释。
4. 区分 `recv_sock_no_ttm`（SYN/ACK 未携带 TTM）、`recv_sock_vip_ttm`（取得 VIP）、`getname_bgw_ttm_ok`（应用取 CIP 成功）、`getname_ttm_empty`（应用取 CIP 失败）。单纯看到模块加载日志不能证明整个链路成功。

编译/加载异常再查 B4，核对 `KERNELDIR`、模块 vermagic、目标内核及编译环境。U2 安装步骤与 modinfo 示例中的模块目录不同，不能固定假设路径。B1/B2 的 bgwagent 分支示例也不同；B3 是云脉 XSC 的补充，不是通用网卡修复步骤。排障中读取编译资料不授权安装依赖、编译部署或卸载/重载模块。

## 6. 文档不足时查源码与内搜

### 6.1 源码取证

从第 2 节选择匹配仓库、分支、commit，通过可用的 iCode 只读能力或已存在的本地 checkout 定位：

- 配置语义：agent 解析/校验 → 控制面接收 → KPD/worker 的实际消费点。
- 输出语义：BGW 返回结构 → agent 显示逻辑，确认查询范围和聚合口径。
- 控制面问题：期望值 → controller 生成配置 → agent 加载结果 → 运行态。
- 统计语义：计数增加位置、计数单位、失败/回退路径；累计值与本轮增量分开。

引用结论时写清仓库、分支/commit、文件、符号和适用范围。本地 AGENTS.md 或 GitNexus 导航约定仍应遵守。未找到源码时标为“未核实”，不要用测试注释代替实现；不要为诊断擅自切分支或修改源码。

### 6.2 企业内部资料搜索

手册缺少专题、controller 来源或线上历史经验时，使用 `/enterprise-search` skill 的脚本搜索，再读取搜索命中的原文。不要优先调用 `infoflow-cli` 做内搜或知识库搜索；如流群聊取证仍按 [infoflow-evidence.md](../infoflow-evidence.md) 执行。搜索摘要只用于发现线索。默认先查 1 页，用 `BGW + 子系统 + 参数/错误` 缩小范围，必要时换 2–3 组关键词：

```text
BGW controller 配置 下发
x86bgw ecv_enable rsecv
BGW offer_hash_flag UDP 健康检查
newttm getname_ttm_empty
```

先加载 `enterprise-search`，按其 HARD-GATE 只调用允许的脚本。内搜和知识库分别使用 `neisou_search.py` 与 `ku_search.py`，脚本路径以该 skill 的 base path 为准：

```bash
python3 <enterprise-search-base>/scripts/neisou_search.py --word "BGW controller 配置 下发"
python3 <enterprise-search-base>/scripts/ku_search.py --word "BGW controller 配置 下发"
```

企业搜索 skill 只允许直接调用脚本，不要拼接 `export`、`cd`、`&&`、`;`、管道或自造参数。内搜结果按 `resultType` 路由：`0/3` 先用 `neisou_fetch.py --resource-url` 获取详情；`16` 转 `ku-doc-manage` 读取知识库文档；其他类型通常只使用摘要。知识库自然语言检索使用 `ku_search.py`，需要详情时遵循其脚本和参数约束。记录标题、URL、摘要、查询词及时间，去掉高亮标签后按 URL 读取原文，核对适用版本和发布日期。

详情获取必须先有搜索结果，不能直接调用 fetch。缺少 token 或认证失败时按 `enterprise-search` 的规定先调用 `get-ugate-token`，不要让用户手动把 token 写入命令行、环境变量、文档或输出。搜索失败、空结果或权限不足时如实记录，继续匹配源码和现场只读取证。

用户给出的 S1 仍是企业内搜接口的协议参考，但正常排障优先使用 `enterprise-search` 的封装脚本，避免重复实现鉴权和错误处理。若脚本不可用，先报告 skill 能力缺失，再依据批准的接口工具和 S1 文档处理；不能自行发明备用参数或绕过认证。

## 7. 诊断输出与知识维护

### 7.1 证据边界

- controller/ACC 运行版本可能不同于代码库默认分支；没有现场版本和生成配置证据时，只能给出待验证方案。
- BGW 管理网到 RS 的探针和 KPD 的 DPDK/BIP checker 使用不同路径；管理网结果不能单独证明线上健康检查、FLAG 聚合或 worker 同步成功。
- Linux 内核网络可达不等于 DPDK 数据面正常；只读查询未发现控制面时，保留“可能存在外部 controller”的限制。

### 7.2 输出

结论明确区分“现场观测”“手册/源码定义”“推断与待验证项”。至少说明子系统、运行版本、关键证据及缺口；不能把源码说明写成现场抓包结果。配置开关、累计计数和健康状态来源不足以闭合根因时，继续缩小只读检查范围或报告证据缺口。

新增参数结论应附来源、适用版本与核实日期，未知内容保留为待核实；这份文档维护知识导航和易误判规则，不复制整份手册或凭据。领域知识归属 tom-diagnose，`tom-project-bgw` 与 bgw-workspace 可作补充（环境事实以 workspace 为准），不能替代此方法入口。
