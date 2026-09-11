# 10-Year Wealth Compounding Architecture — FINAL

**Status: FROZEN**  
**Purpose: Execution, not continual redesign**

## Final Objective

未来10年实现资产大幅升值，同时尽量避免永久性资本损失。

本体系不是为了每天交易、每天预测市场、每天找新币或不断修改参数，而是为了：

> 抓住少数真正重要的财富增长机会，让主要增长引擎长期复利，在极端低估时拥有购买力，在极端高估时兑现部分财富，并避免任何一次错误永久破坏10年复利路径。

最终目标不是预测市场，而是10年后拥有远高于今天的、真正属于自己的、可以安全使用的净资产。

---

## 1. Core Principles

1. NO LEVERAGE.
2. 不要求长期满仓。
3. BTC 是当前 **Primary Growth Engine**。
4. Cash / T-bill 是 **Survival + Optionality Engine**。
5. Structural 逐步建立 BTC 之外的第二长期增长引擎。
6. Asymmetric 用有限风险寻找少量 3x–20x Convex Opportunities。
7. $50,000 Crisis Reserve 永久独立于普通 Portfolio。
8. High-Frequency Observation != High-Frequency Trading。
9. 任何单一 Thesis 错误都不能永久破坏10年复利。
10. 所有复杂策略必须通过真实 Forward Test 证明价值。
11. 少动，让主要增长引擎安静复利。

---

## 2. Portfolio Allocation Engine

不包含 $50,000 Crisis Reserve。

| Layer | BASE | Tactical Range | Hard Limit |
|---|---:|---:|---:|
| BTC Core | 45% | 30%–60% | — |
| Cash / T-bill | 35% | 20%–55% | — |
| Structural | 15% | 0%–30% | Single Asset <=5% |
| Asymmetric | 5% | 0%–8% | Single Asset <=2% |

BASE 是战略中枢，不是每天必须维持的固定比例。普通价格变化不机械再平衡。

BTC 只有在 **Valuation attractive + Liquidity improving + Forced Selling declining + Market stabilizing + no accelerating systemic crisis** 时，才允许向60%上沿移动。

---

## 3. FINAL Allocation Authority

这是所有任务共同遵守的最高权限规则。

完整执行链：

> **Signal -> Allocation Gate -> APPROVED ACTION / ACTION REQUIRED / NO ACTION -> User Execution -> Decision Journal -> Quarterly Audit**

### Scanner 权限

BTC Sentinel、Asymmetric Hunter、Structural Radar 只能：

- 发现信号
- 研究证据
- 提出 Recommendation
- 运行即时 Allocation Gate

它们不能自动移动资金，也不能自动执行交易。

### Allocation Gate

任何真实资金动作，在输出最终操作前必须即时检查：

1. 当前 Portfolio 权重与 Tactical Range
2. Funding Source
3. Risk Budget
4. Correlation Exposure
5. Opportunity Cost vs BTC / Cash-T-bill
6. Fund-Layer Compliance
7. 是否触碰 Permanent Core / Crisis Reserve 等禁止资金层

Gate 是实时的，不等待季度复盘。

### 最终输出只有四类

- **RECOMMENDATION**：值得观察，但尚未达到执行标准
- **APPROVED ACTION**：规则与资金层均通过，建议执行
- **ACTION REQUIRED**：时间敏感的强制建议，必须执行或明确拒绝
- **NO ACTION**：不操作

### User 是唯一执行人

系统没有交易所自动执行权。任何买入、卖出、减仓、止盈、解定存都必须由 User 实际执行。

### ACTION REQUIRED

用于时间敏感风控，例如 BTC Distribution、Asymmetric 强制退出。

触发后不能沉默略过。User 必须：

- EXECUTED，或
- EXPLICITLY REJECTED

两种结果都必须进入 Decision Journal。

### Quarterly Review

Quarterly Review 是 **事后审计、配置健康检查和归因层**，不是实时审批队列，不延迟当下交易。

---

## 4. BTC Core = Permanent Core + Tactical BTC

### Permanent Core

约占 BTC 目标仓位40%–50%。普通周期波动不出售。只有 BTC 长期 Thesis 根本失效时重新评估。

### Tactical BTC

用于：

- 周期低估加仓
- 极端高估减仓
- 流动性周期调整
- 危机后的资本重新配置

BTC 的主要 Return Engine 仍然是 Long-Term Beta，而不是高频择时。

---

## 5. BTC Accumulation Discipline

显著提高 BTC 配置必须联合满足：

1. **Valuation**：MVRV / Realized Price / LTH / 长期趋势偏离等证据支持低估
2. **Liquidity**：Macro / Crypto Liquidity 改善或至少停止恶化
3. **Forced Selling**：Leverage Flush / Liquidations / OI Reset / Exchange Selling / Panic Selling 边际衰竭
4. **Stabilization**：坏消息不再创新低、Spot/ETF Demand 改善、Higher Low、Selling Exhaustion

核心：

> **Cheap != Aggressive Buy**

显著加仓要求：

> **Cheap + Liquidity Improving + Forced Selling Declining + Stabilization + No Accelerating Systemic Crisis**

单笔 Tactical BTC 加仓原则上不超过当时 Cash Layer 的10%–15%，必须分批。

### BTC Buy Cooldown — FINAL PATCH

每次 **User-confirmed Tactical BTC BUY** 后：

- 默认72小时内禁止再次 Tactical BTC 加仓
- Sentinel 可以继续小时级观察，但不能因为同一 Thesis 持续存在而重复 APPROVED BUY

提前解除72小时冷静期只允许在同时满足：

1. BTC 价格出现实质性的进一步向下重定价
2. 四域 Accumulation Evidence 相比上一笔买入发生实质升级

若提前解除，必须输出：

> **COOLDOWN OVERRIDE**

并明确列出新增独立证据。

冷静期只限制 BUY，不限制时间敏感的风险减仓。

---

## 6. BTC Distribution Discipline

四个独立维度：

- Valuation
- Trend Deviation
- Crowding / Leverage
- Marginal Demand Risk

状态统一：

> NORMAL / ELEVATED / EXTREME / UNKNOWN

UNKNOWN 不能视为 NORMAL。

### Action Map

- 0–1 EXTREME -> HOLD / NO ACTION
- 2 EXTREME -> STOP ADDING BTC + DISTRIBUTION WATCH
- 3 EXTREME -> **ACTION REQUIRED: SELL 10%–15% Tactical BTC**
- 4 EXTREME + price still accelerating -> **ACTION REQUIRED: SELL another 10%–20% Tactical BTC**

Permanent Core 不参与普通周期 Distribution。

任何 Tactical BTC 卖出所得进入 Cash / T-bill。不得因为 BTC 继续上涨而 FOMO 买回，只能重新通过 Accumulation Gate。

---

## 7. Cash / T-bill Layer

Cash 不是“闲置资金”。它提供：

- 生存能力
- 熊市购买力
- 危机中的 Optionality
- 降低 Forced Sell 风险
- 等待期间的低风险收益

最低要求：

> 至少12–18个月生活/应急需要 + Strategic Optionality

---

## 8. Structural Layer

目的：建立 BTC 之外的第二长期增长引擎。

候选研究链固定为：

> Structural Change -> Industry Economics -> Value Chain -> Winner -> Value Capture -> Competitive Durability -> Financial Quality -> Reverse Valuation -> Remaining Upside vs Permanent Loss -> Opportunity Cost

只有：

> **HIGH CONVICTION + Reverse Valuation合理 + Allocation Gate通过**

才允许输出 APPROVED ACTION。

配置规则：

- 初始：总资本2%–3%
- 证据增强且估值仍合理：3%–5%
- 单标的 Hard Cap：5%
- Structural Layer 可以长期低于15%，禁止为了达到 BASE 买平庸资产

资金优先来自：

1. Cash / T-bill 高于最低安全垫部分
2. Tactical BTC Distribution 形成的 Cash
3. New Capital

禁止使用 BTC Permanent Core、Asymmetric Budget、$50k Crisis Reserve。

---

## 9. Asymmetric Opportunity Layer

目的：用有限风险寻找少量 3x–20x 机会。

- BASE：5%
- HARD CAP：8%
- Experimental First Entry：0.5%–1%
- Single Asset <=2%
- Same Correlation Bucket 一般 <=4%–5%

每次 BUY SMALL 前必须回答：

> **为什么这1美元现在放这里，比放 BTC 或 T-bill 更值得？**

答不清楚 -> NO TRADE。

禁止自动占用：

- BTC Permanent Core
- Tactical BTC Budget
- BTC Grid
- Ordinary BTC Dip-Buy Cash
- Structural Budget
- Strategic Term Deposit
- $50k Crisis Reserve

### Exit Discipline

- 约2.5x–3x -> Mandatory First Profit Realization，至少 TP25
- 想回收全部本金 -> Sell Fraction = 1 / Current Multiple
- New High + >=2 Leading Evidence Domains weaken -> TP50
- Volume/Social Climax + Next-Day Rollover -> EXIT MOST
- Break Structure Since Entry -> EXIT ALL

时间敏感退出触发时：

> **ACTION REQUIRED**

User 必须执行或明确拒绝，并写 Journal。

---

## 10. Correlation Budget

风险按 Risk Factor 而不是 Ticker 数量计算。

重点检查：

- BTC Beta
- Alt Beta
- AI / Tech Beta
- Energy Beta
- Macro Liquidity Beta
- Single Country
- Single Exchange
- Same Narrative
- Same Chain

避免表面 Diversification、实质 All-in。

---

## 11. Portfolio Risk Governor

最高原则：

> **任何一个观点错误，都不能永久破坏10年复利。**

硬规则：

- NO LEVERAGE
- Asymmetric <=8%
- Single Experimental <=2%
- Single Structural <=5%
- Single Exchange Exposure 一般 <=5%–10%
- $50k Crisis Reserve 严格隔离
- 长期生活资金不得进入高波动资产
- 不因 FOMO 提前释放战略资金
- 不因单一预测 All-in

---

## 12. $50,000 Crisis Reserve

不属于普通 Portfolio Allocation。

正常状态：0 Risk Exposure。

不得用于：

- 普通 BTC 回调
- Grid
- Altcoin
- 普通 Structural 建仓
- FOMO
- 普通熊市 DCA

只有真正系统性金融危机、极端流动性冲击或异常资产错价，经独立重新评估后才进入候选部署，而且必须分批。

---

## 13. BTC Sentinel

定位：Risk / Liquidity Detection Layer。

运行：

> **Hourly Detection + Low-Frequency Portfolio Decision**

任务负责感知，不负责自动交易。

达到资本动作标准时必须先过 Allocation Gate。

Distribution 3/4 EXTREME 属于 ACTION REQUIRED，但仍由 User 执行。

---

## 14. BTC Grid

定位：Small Trading Laboratory，不是主要 Return Engine。

管理：

> **Weekly Review**

Boundary 条件只在每周 Review 正式评估，不作为单独的盘中频繁调参触发器。

BTC Sentinel 在周中只能提示 Grid Risk；除真正 Capital-Safety Emergency 外，不得导致反复中途改参数。

12–24个月验证：

> Net Benefit after Fees / Slippage / Opportunity Cost > Simple BTC + Cash

无法证明则关闭 Grid 实验。

---

## 15. Opportunity Cost Rule

任何新增资产、增加风险敞口或跨层调资金前必须回答：

> **为什么这1美元现在放这里，比放 BTC 或 T-bill 更值得？**

Scanner 可以进行机会成本自检，但最终是否允许资金动作由实时 Allocation Gate 裁决。

---

## 16. Unified Decision Journal — FINAL PATCH

Single Source of Truth：

> `decision-journal.json`

Journal 是 Append-Only。历史 rationale 不允许用未来信息改写。

必须记录：

- Tactical BTC BUY / SELL
- Asymmetric BUY / ADD / TP / EXIT
- Structural BUY / ADD / REDUCE / SELL
- Strategic Term Deposit material decision
- Crisis Reserve state change
- Any ACTION REQUIRED rejection

每条至少记录：

- Timestamp
- Asset
- Price
- Signal Source
- Allocation Gate result
- Action
- Amount / Weight
- Funding Source
- Evidence Available At That Time
- Alternative Action
- BTC Alternative
- Cash/T-bill Alternative
- Correlation Bucket when relevant
- Opportunity Cost conclusion
- Invalidation / Thesis Kill
- Cooldown Status when relevant
- Executed / Rejected / Pending
- Later Outcome

如果任务无法完成持久化写入，必须输出：

> **JOURNAL WRITE REQUIRED**

禁止假装已经写入。

---

## 17. Decision Journal Immutability

FIRST-SEEN、当时证据、当时理由、当时 Gate 结论全部不可事后改写。

未来结果只能作为 outcome append。

目的：消灭 hindsight bias 和事后叙事。

---

## 18. Performance Attribution

每季度尽可能拆解：

- BTC Beta
- BTC Tactical Allocation
- Cash Yield
- Cash Drag
- Structural Return
- Asymmetric Return
- Grid Return
- Fees
- Slippage
- Realized Tax Cost

没有足够数据时：

> UNKNOWN / NOT YET MEASURED

不得伪造 Alpha、Sharpe、Maximum Drawdown、Win Rate。

---

## 19. Benchmarks

长期比较：

1. 100% BTC Buy & Hold
2. 60% BTC / 40% Cash，Annual Rebalance
3. 50% BTC / 30% Broad Equity Index / 20% Cash
4. Actual Strategy

使用：

> Incremental Strategy Return vs Benchmark

不要未经证明称为 True Alpha。

---

## 20. Quarterly Portfolio Review

Quarterly Review 只负责：

- Current Allocation
- Range Violations
- Risk Governor
- Decision Journal Completeness
- Return Attribution
- Benchmark Comparison
- Rule Violations
- Next-Quarter Necessary Actions
- Keep-As-Is Items

它不延迟实时交易，不做实时审批。

如果 Journal 不完整：

> **JOURNAL INCOMPLETE**

不得凭记忆补造历史。

---

## 21. Custody / Counterparty / Tax

长期 BTC 不应全部放在单一交易所。

- Exchange 主要用于交易与执行
- Long-term BTC 避免 Single Point of Failure
- Single Exchange Exposure 一般 <=5%–10%
- 大额长期 BTC 需要独立安全域、可靠离线备份和恢复测试

大额 TP / EXIT / Bank Transfer 前：

> **TAX / LEGAL CHECK = REQUIRED**

保留 Exchange Statements、Wallet History、Cost Basis、Bank Transfer、Source-of-Funds Evidence。

---

## 22. Forward Test

未来12–24个月验证：

### BTC Tactical
是否改善成本、卖出纪律和资本效率？

### Asymmetric
是否真的提前发现赢家？

追踪：FIRST-SEEN、Pre-Move Capture、MAE、Realized Capture Ratio、False Positives、Missed Winners。

### Structural
是否逐步成为第二 Growth Engine？

### Grid
是否在费用和机会成本后仍有真实 Net Benefit？

### Sentinel
是否减少重大错误，而不是增加交易次数？

不能证明价值的模块：

> DOWNGRADE / SIMPLIFY / REMOVE

---

## 23. Frozen Rule

禁止因为以下情况创建 V3 / V4 / V5：

- 一次市场事件
- 一个新指标
- 一次错过上涨
- 一次卖早
- 一个新的 AI 建议
- 想让系统“更聪明”

只有两种情况允许修改架构：

1. Forward Test 证明系统性失败
2. Personal Situation materially changes，例如资产规模、收入/生活成本、税务身份、国家、家庭责任、投资目标、流动性需求发生重大变化

本次 **Allocation Authority + BTC 72h Cooldown + Unified Decision Journal** 属于修复已识别结构性缺陷，是 FINAL 内部补丁，不构成新版本。

---

## 24. Final Operating Principle

> 少动。让 BTC 主引擎安静复利。不要因为跌了就盲目抄底。在真正低估、流动性改善、强制卖压衰竭、市场开始企稳时增加风险。不要试图预测绝对顶部。在估值、趋势、杠杆和边际需求共同进入极端状态时，兑现部分 Tactical BTC。保留 Permanent Core。让 Cash 成为危机中的购买力，而不是心理负担。只用有限资金寻找极端赢家。让 Structural 逐步成为第二增长引擎，而不是为了配置而配置。让赢家最终变成真实财富，而不是 Round-trip。保护 Seed、Custody、税务记录和资金来源证明，因为投资收益只有安全地属于自己才算真正的财富。永远避免一次错误摧毁10年的复利路径。

**Execution Focus from now on:**

> **EXECUTION -> JOURNAL -> ATTRIBUTION -> BENCHMARK -> QUARTERLY / ANNUAL REVIEW**
