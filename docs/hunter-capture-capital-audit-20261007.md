# Hunter 七币、AXS ADD 与资金语义调查（2026-10-07）

固定证据 snapshot：`b14e832234388f60f3a521276b36b2d17cd24f7c`。组合 generation：`MONITOR_20261006T234514978552Z`（06:45 +07 的真实 Monitor）。Research generation：`20261006T231710989740Z`。所有数量和记录只对该 snapshot 有效，不冒充未来最新状态。

所有交易均为 SHADOW；账本内 SELL/PnL 是已有模型记录，不是真实交易或历史可成交证明。报告只读，不补写 BUY/SELL，不修改策略。

## 七币账本捕捉情况

| 币 | 层 | BUY（越南时间 +07）/参考价 | SELL（+07）/账本净 PnL | 证据结论 |
|---|---|---|---|---|
| GAIB | V1 | 未发现 | 未发现 | 该账本未发现进入记录 |
| GAIB | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |
| MOVR | V1 | 10-04 20:02:36 / 1.834 | 10-05 17:14:43 / 241.59U | KNOWN：账本事件 |
| MOVR | V2 | 10-05 14:06:25 / 1.893 | 10-05 17:14:43 / 202.59U | KNOWN：账本事件 |
| FLUID | V1 | 未发现 | 未发现 | 该账本未发现进入记录 |
| FLUID | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |
| NIGHT | V1 | 10-04 16:53:51 / 0.05001 | 10-06 07:05:13 / 22.23U | KNOWN：账本事件 |
| NIGHT | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |
| CAP | V1 | 未发现 | 未发现 | 该账本未发现进入记录 |
| CAP | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |
| VTHO | V1 | 10-04 21:50:14 / 0.000653 | 10-06 07:15:09 / 55.47U | KNOWN：账本事件 |
| VTHO | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |
| FIL | V1 | 10-05 00:36:18 / 1.046 | 10-06 05:36:48 / 128.29U | KNOWN：账本事件 |
| FIL | V2 | 未发现 | 未发现 | 该账本未发现进入记录 |

两份 portfolio 的当前 event history 未声明截断；此结论仍只覆盖现有账本，不能断言账本外从未发生过。

## 峰值与控制组的证据边界

| 仓位 | holding peak / 持仓 MFE | full-opportunity peak | 限制 |
|---|---|---|---|
| V1 MOVR | 2.387 / 30.1527% | 2.39 | HISTORICAL_BACKFILL；observation_complete=False |
| V1 FIL | 1.2046 / 15.1625% | 1.211 | HISTORICAL_BACKFILL；observation_complete=False |
| V1 NIGHT | 0.05265 / 5.2789% | 0.05265 | HISTORICAL_BACKFILL；observation_complete=False |
| V1 VTHO | 0.000725 / 11.026% | 0.000782 | HISTORICAL_BACKFILL；observation_complete=False |
| V2 MOVR | 2.387 / 26.0961% | 2.39 | HISTORICAL_BACKFILL；observation_complete=False |

MOVR、NIGHT、VTHO 是已有 Profit Protection SELL 记录的 control group；FIL 为 PROFIT_REVIEW_MOMENTUM_FADED。上述记录不是 PR #46–#54 后的新 ARM/EXIT 样本。

KNOWN：买卖事件、记录的价格和账本 PnL。INFERRED：带 HISTORICAL_BACKFILL 的持仓峰值/MFE。UNVERIFIABLE：当时完整盘口、滑点和可执行利润窗口。full-opportunity peak 可能在 SELL 后，不能当作实际持仓利润。

ENA/PENDLE 未添加任何历史 SELL；原窗口仍为 `UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW`。该 snapshot 的全部10个 V2 open positions 仍 UNARMED；结论 `RUNTIME_ARM_EXIT_SAMPLE_PENDING`。

## Bybit-only 当前卡点（不是对过去所有周期的解释）

当前官方 Discovery 有 GAIB、FLUID、CAP 的 Bybit Spot 记录。GAIB 在本 Research generation 为 WATCH，score=1.0872、independent signals=1。FLUID/CAP 进入研究候选，但候选明确 execution_supported=false。

| 候选 | 当前 Research/Capital Review blockers |
|---|---|
| FLUID | VENUE_SPECIFIC_EXECUTION_AND_MONITOR_NOT_INTEGRATED, LIVE_ORDERBOOK_MISSING |
| CAP | VENUE_SPECIFIC_EXECUTION_AND_MONITOR_NOT_INTEGRATED, ASSET_IDENTITY_NOT_CORROBORATED, LIVE_ORDERBOOK_MISSING, TOO_FAR_ABOVE_DISCOVERY_FOR_REMAINING_RR |

`VENUE_SPECIFIC_EXECUTION_AND_MONITOR_NOT_INTEGRATED` 与 `LIVE_ORDERBOOK_MISSING` 是当前完整 Bybit 执行链缺口。Phase 1 的 Bybit public WS/REST observation 并不解锁正式 BUY。不能通过 Binance 同名资产或混合 Venue 深度绕过该限制。V1 同一 Research generation 的 early_sample_exclusions.rows 对 FLUID/CAP 也明确记录 VENUE_SPECIFIC_EXECUTION_AND_MONITOR_NOT_INTEGRATED。这是执行链缺口，不是把 V2 的 Supply/RR/Identity 全部严格门槛套到 V1。对其他历史周期没有证据时不得反推相同原因。

## AXS ADD 链路

当前 AXS 仅有 V1 position `SHV1-20261004T105916-AXS-3600e0`，V2未发现 BUY/ADD。

| 事件 | 时间 +07 | 参考价 | 累计模型本金 |
|---|---|---|---|
| SHADOW_V1_BUY | 10-04 17:59:16 | 1.396 | 1000U |
| SHADOW_V1_ADD | 10-04 18:38:14 | 1.391 | 2000U |
| SHADOW_V1_ADD | 10-04 18:41:39 | 1.393 | 3000U |

历史第二笔比第一笔仅低约0.358%；第三笔比第二笔高约0.144%，且相距约3分25秒。两次旧 ADD 的 reason 均为 LOWER_PRICE_FULL_REVALIDATION；标签不能证明当时满足后来新增的 gate。

当前代码 `research/hunter_shadow_trader_v2.py:add_material_improvement_required_pct/add_fresh_evidence/add_recovery_confirmed/decision` 已有上一 tranche 更低价、至少0.75%动态改善（第三 tranche提高）、新 evidence/generation、独立15分钟证据窗口与恢复确认。现有 `tests/test_hunter_shadow_trader_v2.py` 已在508项全套回归/CI中真实执行。

代码/测试：已有防止该模式的 gate，本报告不重复修改。真实新 ADD acceptance：仍需新样本；AXS已经3个tranches、之后没有新ADD，不能用它证明 gate 在新机会下的实际效果。不得重写这两笔旧 ADD。V1 Broad Discovery / EARLY 定位不变。

## 20K / 17K / 3K / Tail 4K

| 字段 | snapshot 值 |
|---|---|
| capital_pool | 20000 |
| ordinary_opportunity_cap | 17000 |
| strategic_reserve | 3000 |
| ordinary_used | 14000.0 |
| strategic_reserve_used | 0.0 |
| strategic_reserve_available | 3000.0 |
| used_capital_usdt | 14000.0 |
| max_deployable_usdt | 12477.47 |
| allocator_available_usdt | 0.0 |
| tail_budget_role | STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION |

Tail calibration `effective_tail_cap_usdt=4000.0` / `CALIBRATION_CEILING_NOT_FINAL`；其角色是 `STRESS_DIAGNOSTIC_NOT_CAPITAL_ALLOCATION`。4K是stress scenario参数，不是另一个资本保留池，也不是3K储备的替代品。

当前RISK_OFF利用率使max_deployable低于used capital，allocator_available=0不表示储备不存在。3K仍在20K内、该snapshot未使用。已实现利润参与权益显示，但20K hard cap不提高；不可按Tail4K或Research capital_state的默认available字段理解正式可买资金。最终capital allocator以fresh portfolio为准。

## 本轮 MTM runtime 验收

PR #54后自然 Monitor：generation `MONITOR_20261006T234514978552Z`。realized=795.79U；reference open unrealized=-626.67U；same-depth exit cost=24.34U；full-quantity liquidation net MTM=149.72U。

MTM使用完整quantity深度的净退出估算；cost使用同一深度mid/VWAP/fee。独立ticker参考价与深度mid可能不同，不能再次机械用reference unrealized减该cost验证等式。closed_win_rate=100%仅指已平仓样本；不是整个组合100%盈利。

安全边界：real_trading_enabled=false；capital_authority=NONE_SHADOW_ONLY；scheduler real_order_count=0。

未完成：Fast Watch服务器部署/官方真实连接和接管、至少24h A/B、新ARM/EXIT与ADD样本、Bybit完整正式执行链、历史盘口可成交证明、7天观察。报告不把UNKNOWN改成PASS。
