# Sentinel 分析迁移候选

状态：离线分析契约与授权模型命令适配器候选。未启用、未部署为正式分析、未取得状态 writer，不能用此文件宣布 Sentinel 迁移完成。

`scripts/sentinel_analysis_candidate.py`：

- 固定现有六个 Leading Warning 主状态与五个早期动作，不以突破确认代替主判断。
- 绑定冻结策略、证据、旧主状态、main 提交和输入哈希；输出必须匹配本轮输入。
- 全部六类域都需显式评估；缺失为 UNKNOWN。证据域由采集来源固定映射，不能把 OI/funding、多个价格周期重复计为独立证据。
- 方向预警至少两个独立域且含非价格域；options 只作修饰。错误、过期、无时区或未知来源不得支持方向预警。
- `first_detected_at` 仅在主状态未变时保留，防止事后改写最早发现时间。
- 日期型 ETF/Treasury 保留为上下文，不伪装盘中新鲜度；完整日期/交易日判定及更广的数据域仍需接入。
- 输出只能写仓库之外。canonical_write_allowed、candidate_enabled、real_trading_enabled、新资本动作始终为 false。
- `run_model_command` 只接受显式授权 executable argv，stdin 传绑定输入；不用 shell、没有默认供应商、不扫描凭据、不输出 stderr、不自动重试模型计费调用；进程组超时终止，输出大小限制 64KiB。

41 项 Sentinel 回归通过：25 项原采集/fallback/watchdog/CAS 测试，16 项新候选测试覆盖输入篡改、旧响应绑定、过期输入、域重复计数、错误来源、缺域、越权输出、秘密错误输出、超时与过大输出。模型经济结论的质量未由这些测试证明；没有把 fixture 当真实模型运行。

## 实际执行与剩余必要步骤

先从真实 main 构造 `snapshot(policy, evidence, previous, source_sha, now)`；仅将实际采集的证据输入显式授权的模型命令，再用 `validate_proposal` 验证返回。可用 CLI 的 `--proposal` 进行离线回读验收，或用 `--model-command-json` 接受已配置的模型程序。`--output` 必须位于仓库之外。不得用人工 fixture 作为真实生产分析。

服务器当前没有可调用的授权模型命令/模型环境文件。当前没有部署模型 API 凭据或发出计费请求；也没有提取 ChatGPT 会话凭据来替代 API 授权。需要安全的账户/项目选择、服务器授权模型配置和真实调用验收。

这不是完整分析引擎的全部迁移：宏观盘中与 DXY、清算、holder/supply 等数据仍有缺口；现有分析与新模型的方向、证据引用、资本门槛与故障行为需对照验收。`sentinel-runtime.json` 与旧任务保持原状。只有合法模型实际执行、完整策略与数据门槛通过、CAS/main 回读完成、单 writer 交接明确，并观察两个连续真实小时完整周期后，才可另行提升运行状态并关闭旧任务。

不得把 `OFFLINE_PROPOSAL_VALIDATED` 等同于 PERSISTED、策略改善、消息送达或正式迁移完成。
