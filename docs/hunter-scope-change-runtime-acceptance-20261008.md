# Hunter 继续执行与范围变更验收 — 2026-10-08

结论：**PARTIAL_FIX**。本轮已经修改、测试、合并、部署并实测恢复；没有宣称全部验收完成。Sentinel 完整迁移已由用户取消，现有 ChatGPT Sentinel 分析和服务器 evidence 采集保留，不再作为 Hunter 的待办或权限阻塞。

## A. 权威基线与提交

发布前再次读取最新 main：7d318cf7bcf35038ff4af1c24482b645df7d27c5；状态提交与代码修改分别记录。

本报告数据固定读取 GitHub main `d232f7457390aad800c482d5f9abd6bcd492187a`；运行提交持续增加，因此该 SHA 是本次快照，不是保证永远不变的 main。修改前分别重新 fetch/读取最新 main 并核对修改文件的 Git blob，API 发布只包含选定代码/文档路径，正常 PR 合并，未覆盖其他窗口或服务器状态提交。

| PR | 已合并代码 | 修复 | 验收边界 |
|---|---|---|---|
| [81](https://github.com/leo14881-eng/btc-grid-state/pull/81) | `8a6c35dd78713ad687067c318e84a30c0846e563` | 生命周期/Fast Watch 共享同 Venue、同手续费、完整持仓深度现金流；Bybit 未知成本 fail closed | 测试通过、main 精确回读；常驻新版本实际加载见下文 |
| [82](https://github.com/leo14881-eng/btc-grid-state/pull/82) | `c4f27e4707d4c516ce11af08ab4441b1f42d72da` | 原 authoritative Monitor 接入 Bybit Spot primary evidence，禁止混 Venue、旧证据覆盖；同一个 writer | 真实 Monitor 已加载；当前没有实际 Bybit primary 持仓样本 |
| [83](https://github.com/leo14881-eng/btc-grid-state/pull/83) | `09e7e3a4985a9371bbf652709bbee3d152b94058` | 辅助任务 CAS 冲突最多三次最新源完整重算；Watchdog 补上 Discovery 的严格恢复资格 | 服务器 wrapper 安装、校验、恢复 Discovery→Research→回读实际成功 |

PR #46–#50 的 merge SHA 已重新读取 GitHub，均为本次 main 的祖先；对应持久化 lifecycle、health ordering、连续计数、20K/17K/3K、MTM 代码存在并通过当前测试，标 **ALREADY_FIXED**，没有重复重写。HAEDAL/NONE active counter 修复也已存在。

修改文件：81 修改 research/hunter_lifecycle_state.py、research/hunter_fast_watch.py，新增成本测试及范围说明；82 新增 research/hunter_bybit_management.py，修改 research/hunter_position_monitor.py、research/hunter_shadow_trader_v2.py，新增管理测试及说明；83 修改 scripts/hunter_scheduled_job.sh、scripts/hunter_job_runner.py、tests/test_hunter_aux_cas_recovery.py，新增 tests/test_hunter_aux_fresh_retry.py 和恢复说明。未修改 BUY 门槛、核心选币、Profit Protection 参数、Loss Recovery 策略、V1 Broad Discovery 或 Sentinel 策略。

## B. 问题分类

- **CONFIRMED_BUG / FIXED**：Bybit 原有持仓只能 HOLD，无法在自身 fresh evidence 上进入同一生命周期管理；共享成本模型及 authoritative Monitor 接入已实现。Mixed Venue、未知 fee/depth、旧 event/generation 均拒绝，Bybit 失败不污染 Binance 仓位。
- **CONFIRMED_BUG / FIXED**：01:17Z Discovery 运行期间代码被 PR82 更新，CAS 正确拒绝旧源，但辅助 launcher 不会完整重算；旧 Watchdog 恢复 allowlist 也没有 Discovery。83 保留 CAS 并增加有界最新源重算及严格恢复。
- **OBSERVABILITY_GAP / FIXED**：常驻 Fast Watch 先前仍加载 403cbf8b，不能拿 main/账本 source 冒充加载版本。本轮实际更新代码指针、重启、读 health，已加载 86e2a33b。
- **NOT_A_BUG**：RISK_OFF 的 allocator_available=0；3K Reserve 仍存在。旧 base checkout/cached ref 不等于 systemd isolated generation 的运行 source。
- **RUNTIME_PENDING**：完整新自然小时链、新 Bybit primary 持仓、新真实 ARM/EXIT 和交易通知 ACK、最新代码连续24h及足够 A/B/holdout 样本。

## C. Recovery / HUMA / HAEDAL

本次10个 V2 open positions 的 health/recovery/snapshot generation 都是 `MONITOR_20261008T014015369280Z`。STRONG 与恶化 reasons 冲突数=0，generation 混用数=0，NONE active counter 残留数=0。IO=STRONG/NONE，两个 active counter 均0；HUMA 和 HAEDAL 当前均 WEAKENING/LOSS_RECOVERY、两个 active counter 均0。历史 transitions 保留，不能把历史 RECOVERED 当作当前 active recovery。

Loss Recovery 保留连续 fresh generation 的 persistent invalidation/recovery quality/资本锁定诊断，没有新增自动亏损减仓。对此准确结论为 **OBSERVABILITY_FIXED_BEHAVIOR_UNCHANGED**；不是宣称已经自动释放亏损仓。THESIS_INVALIDATED/PERSISTENT_INVALIDATION 本身不等于立即 SELL。

## D. Journal 与部署版本

Shadow Server Ops“币圈服务器”实际 service_logs 读取 Monitor、Discovery、Research、Watchdog，returncode=0，能读到真实 stdout/stderr、CAS 错误及成功回读。没有再遇到 journal 权限拒绝；没有给连接器 root 写权限。

| 版本字段 | 实际值 |
|---|---|
| base_checkout_sha | `19b7c6bbe63a5f5741f051cf62a5d469290d2703` |
| cached_origin_main_sha | `b661483e05ad7c3491a89715b47c88f44bc0fd36`，只读 cached，非实时 GitHub |
| discovery published source SHA | `4fce69157442c4370a5a6896c2b8b1c8baa656d8` |
| research published source SHA | `297b75c0350997cc7bc1eb830d10224cc6b8ab10` |
| monitor start source SHA | `86e2a33be462bfd0074e04762f88108ff6b9c821` |
| watchdog published source SHA | `092dc9bd5b3ce0d259edaab692c825a604b6ca95` |
| authoritative main readback SHA | `eb97330075456c8e22ca799c0ce7608cdf9f2205` |
| Fast Watch 实际 loaded code | `86e2a33be462bfd0074e04762f88108ff6b9c821` |

Discovery/Research/Watchdog 的 source_head 字段是任务 published job HEAD；开始时 source 没有独立字段，provenance 已明确标注。Monitor source 由 scheduler.state_revision 确认，不能混称。以上运行版本包含81/82/83；报告 JSON 保存原始 provenance。

Scheduled wrapper 实际于01:31:55.348238Z安装，SHA256 `b557817efd5dbe68426eccdf42a88aa6716e5714ff8f7c22f367ba6c0cd2eefe`，安装前旧 SHA guard、备份、bash -n、原子替换、内容回读均通过。Fast Watch 实际01:39:15Z重启，旧 release 和运行档案保留，DynamicUser/NoNewPrivileges/只读代码及受限 runtime 写路径保留。

## E. 完整链与真实定时

**必须保留的失败**：01:17:00Z自然 Discovery → 01:17:22Z AUX_CAS_REJECTED_STALE_WRITER → 01:17:28Z FAILURE/readback；Research 未启动。不能把后来恢复改写成该自然周期成功。

**USER_AUTHORIZED_RECOVERY 已实测成功**：01:31:55Z启动新 Watchdog，它实际请求 Discovery；Discovery 01:32:04.672716Z–01:32:27.563502Z完成、systemd01:32:33Z成功退出。generation=`20261008T013206955874Z`。Research 01:32:37.894047Z–01:36:56.085875Z，33 steps 全部 SUCCESS，包括资本决策、V1/V2、generation guard、Single Writer lock、persist 与 main readback；readback=`297b75c0350997cc7bc1eb830d10224cc6b8ab10`，main_readback_verified=true，V2占用14K。

| 自然5分钟周期 | 开始 / 分析完成 UTC | source | readback / 结果 |
|---|---|---|---|
| 01:20:00Z | 01:20:08.639970 / 01:20:58.202441 | aafcf07b544dd8ad4666305e210f2e7f76a0756b，含81/82 | 115f010b82a5a5497ac848914dfb8ed22161a4be；01:21:12服务SUCCESS；missed0/duplicate0 |
| 01:25:00Z | 01:25:05.735440 / 01:25:51.878832 | 5379e89c7e4ebf4d257d572fc8c842a80fec60ac，含81/82 | 76536fcd476968b9878faa65b5cd09446f4394ef；01:26:06服务SUCCESS；missed0/duplicate0 |
| 01:35:00Z | 01:35:03.868096 / 01:35:51.344064 | 12081bf166da42f8dcfc5c8ad33f55fdb9f1273f，含81/82/83 | 491c4a21fb0ee5abb8122defb754543116cce5cd；SUCCESS；missed0/duplicate0 |
| 01:40:00Z | 2026-10-08T01:40:09.751055+00:00 / 2026-10-08T01:40:56.117669+00:00 | 86e2a33be462bfd0074e04762f88108ff6b9c821，含81/82/83 | eb97330075456c8e22ca799c0ce7608cdf9f2205；01:41:10服务SUCCESS；missed0/duplicate0 |

追加验收：新Fast版本加载后，01:40与01:45两个连续自然Monitor都SUCCESS。01:45 generation=2026-10-08T01:45:00Z；开始01:45:05.663350Z，分析完成01:46:00.902989Z，服务回读成功01:46:15Z；source=c174502a6c0dba46c3e6944ce3e0c0ae1f7c038c，missed0/duplicate0。补充health/readback SHA见JSON，组合数字仍按上述固定快照，未混用两个时刻。

01:37Z自然 Watchdog SUCCESS。01:31Z自然 Blind Replay SUCCESS，实际 source/重试不能冒充新 Profit Protection 可成交历史。当前 scheduler HEALTHY；5分钟 Monitor/小时链保留。**修复后的完整新自然小时周期仍需02:17Z或以后真实定时证据**；恢复周期不替代它。

## F. Fast Watch / REST / Profit Protection

加载新代码后，Binance expected=actual=11（BTC+10 V2）；Bybit=0/NOT_REQUIRED，不能宣称 Bybit 生产持仓验收。health=PARTIAL_FAST_PATH_DEGRADED；Binance WS stale 时 REST 正常接管，并未把 authoritative Monitor 标 FAILED。formal_writer=false，保持 OBSERVATION_ONLY，不修改正式持仓，不每tick写 GitHub。

截至 2026-10-08T01:42:54.096176+00:00：Binance 累计event=11683373，connection=5，reconnect=0，stale=4427，fallback=4238，duplicate drop=0，out-of-order drop=377991，历史both-source失败计数=1。这些计数跨重启恢复，**不是最新代码独立24h统计**。当前进程WS uptime约99.035%，价格检测中位约124.227ms，REST接管中位约237.053ms；观察窗口不足24h，不能外推。

正式10个open positions 的保护状态仍 UNARMED。ENA/PENDLE 的旧MFE不能反算为实时已ARM、不能补写盈利SELL；历史结论保留 **UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW**。当前为 **RUNTIME_ARM_EXIT_SAMPLE_PENDING**。arm/exit improvement、missed windows、capture delta 均UNKNOWN/null，review_count=0时 false_fast_trigger_count=0不能证明没有误报风险。

最新加载代码连续24h最早2026-10-09T01:39:15Z（+07 08:39:15）。已建立只读复查，实际schedule=2026-10-09T01:40:36Z（+07 08:40:36）；原10月8日15:50+07检查也保留、明确新版时间不足不得通过。3–7天/7天研究观察不能压缩成一个周期，不得自动启用正式Fast writer。

## G. 当前组合与资金

数字来自同一固定main、`MONITOR_20261008T014015369280Z`，as_of=2026-10-08T01:40:35.720282+00:00，均为 shadow 模型，不是真实成交。

| 指标 | V2 | V1 |
|---|---:|---:|
| realized_net_pnl_usdt | 795.79 | 7327.97 |
| open_unrealized_pnl_usdt | -1299.63 | -11750.31 |
| estimated_exit_cost_usdt | 23 | 305.94 |
| mark_to_market_net_pnl_usdt | -535.19 | -4682.66 |
| open positions | 10 | 157 |
| closed win rate | 100.00%（19 closed） | 90.58% |

V2 open_loss_exposure=14000；thesis_invalidated=5；loss_recovery系=9，其中PERSISTENT_INVALIDATION=5。19/19 closed wins 不表示整个组合赚钱，当前MTM为负。MTM使用full-quantity depth net，exit cost使用同深度receipt mid；reference mark时刻/价格不同，不能机械用reference浮盈减exit cost代替execution MTM。

资本：pool=20000，ordinary cap=17000、used=14000、available=3000；reserve=3000、used=0、available=3000。Tail4K只为 `STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION`。RISK_OFF max_deployable=12477.47、allocator_available=0，不是Reserve消失，也不允许突破20K。

## H. 测试、通知与其余边界

本地完整630/630 PASS，无skip；服务器Research完整630项执行，629 PASS/1 skip，无失败（测试输出如实保留）；服务器release另外134 Fast Watch +9成本测试 PASS；83安装前7真实临时Git launcher+17恢复资格测试 PASS。新增测试覆盖同Venue/fee/depth、ARM→峰值→giveback/负gap、重启/dup/旧generation、BN与Bybit隔离、CAS真实新源重算、最多三次、鉴权/数据失败不重试及同job并发排他。CI81/82/83均SUCCESS；所有代码文件合并后精确main回读通过。

V2交易通知任务enabled，只发送cutoff `2026-10-06T18:26:30Z` 后新BUY/ADD/SELL（具体SELL reason带止盈/卖出/止损语义），不发送所有持仓。本次64个历史events中cutoff后合格事件=0，没有理由补发旧交易。新事件真实delivery ACK仍待样本；queued/prepared不等于送达。旧Sentinel任务enabled，最近真实运行01:30:18Z，完整迁移取消不等于关闭它。

PR35 Draft保留未启用：此前隔离候选78tests PASS、历史809reads无失败，但完整可执行窗口数0，独立holdout和整体改善不能证明，结论NOT_ENOUGH_EVIDENCE_TO_REPLACE_CURRENT_V2。ENA/PENDLE/HUMA replay及control已有仓库审计文件，本轮不伪造历史执行。七币/AXS审计见 docs/hunter-capture-capital-audit-20261007.md：这是账本捕捉审计，不是已证明没有任何错过盈利机会。

**未实施的相关边界，不能写成只待运行**：小时 Bybit 新BUY/ADD admission、Bybit同Venue历史/closed post-exit回填没有实现；本轮 Phase1 不迁移BUY/ADD决策、不改核心门槛，未擅自解锁这些入口。现有Bybit持仓管理接入不能冒充所有Venue完整交易链。

**仍未验收**：新自然完整小时链；最新代码24h/3–7天A/B；真实Bybit primary持仓；新真实ARM/EXIT/full-depth/event/persist/notification一致性；新交易通知delivery ACK；PR35完整执行replay/独立holdout/整体改善。原因分别是自然周期/观察时间未到、真实条件无样本、历史深度缺失或实现范围未包括，不能伪造。

## I. 安全结论与独立后台

real_order_count=0；real_trading_enabled=false；capital_authority=NONE_SHADOW_ONLY；Fast formal_writer=false；无真实订单API、用户真实资金操作。V1继续Broad Discovery/EARLY，20K硬上限保持。服务器服务与定时器独立继续运行，验收自动化已实际建立；聊天结束不代表Codex还在后台持续开发代码。

最终结论：**PARTIAL_FIX**。证据明细：docs/replays/hunter-runtime-acceptance-20261008-scope-change.json。
