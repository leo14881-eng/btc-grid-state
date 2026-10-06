# 股票独立调度与失联恢复

本模块只负责基础设施。V3 BUY / ADD / SELL、手续费、资金规则和交易日历 Gate 不变；仅影子模拟，不接触 Alpaca 密钥或真实订单。

## 已确认的问题与证据边界

2026-10-05 18:04—18:57 UTC，股票监控没有自动新运行。工作流启用、默认分支 main、Actions 权限正常，且没有排队任务；同仓库其他定时工作流也出现大间隔。手动运行 37359843404 用时 29 秒，112 项股票测试、26 项 P0 验收通过，327 个持仓更新、4 笔影子 SELL 并写回。

能够确认缺口在 GitHub 定时触发环节；无法从公开 API 证明其内部丢失/延迟的具体原因。GitHub 官方说明 schedule 可能延迟或丢弃：https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule 。单次 push 或手动成功不能证明连续调度正常。

## 工作方式

- Cloudflare Cron 每 5 分钟（UTC 03、08……58）唤醒独立 Worker，原 GitHub cron 保留备用。
- 固定仓库 `leo14881-eng/btc-grid-state`，固定 main，只允许触发 `stock-shadow.yml` 和 `stock-shadow-position-monitor.yml`。
- 读取同一 Git commit 的交易日历缓存、主扫描摘要、监控健康记录。报价失败、缺失股票或部分刷新不计为成功心跳。
- 开市监控超过 10 分钟无完整成功刷新，健康状态标记 stale；优先补跑监控。主扫描满 60 分钟需要刷新；持续 PARTIAL 时，两个 API 已接受的 monitor 派发后让到期主扫描执行一次，避免长期缺失单个报价导致主扫描永远饥饿。主扫描执行不清除 stale；429、超时和 active 二次检查拦截不计作恢复次数。
- 周末不补跑持仓监控；主扫描仍可进行研究。节假日/提前休市使用 Alpaca 日历缓存；日历未知时可以触发现有引擎查询日历，实际交易授权始终由引擎最后一道 Gate 决定。
- 检查 queued / in_progress / waiting / pending / requested 的任务，存在股票主扫描或监控时不重复触发。库存超过分页可验证范围时停止补跑，不猜测“没有任务”。
- Durable Object 事务保存 5 分钟派发冷却，保护并发唤醒和进程重启；POST 超时也保留冷却，避免立即重复提交。GitHub 内置 cron 与外部调度仍存在 API 可见性窗口，最终账本写入继续由共同 concurrency 和持久化版本检查保护。
- `/health` 只读，心跳过期、API 错误、开市监控 stale 返回 HTTP 503。公开 HTTP 请求不能触发补跑。健康状态及最近 32 条审计记录持久化；Workers 日志记录结果，不输出 Token 或 GitHub 响应正文。
- 调度器自身不是实时交易引擎；它仍依赖 GitHub Runner 和原有延迟行情。Cloudflare 调度也不是绝对准点保证。

## 部署（独立 Worker，不修改 Bybit 行情 Worker）

配置文件：`wrangler.jsonc`；入口：`worker.mjs`。

仓库提供仅手动触发的 `.github/workflows/deploy-stock-shadow-scheduler.yml`。使用三项独立的仓库 secrets：`CLOUDFLARE_API_TOKEN_STOCK`、`CLOUDFLARE_ACCOUNT_ID`、`STOCK_SCHEDULER_GITHUB_TOKEN`；最后一项会作为 Worker 的 `GITHUB_ACTIONS_TOKEN` 安装。不要复用或修改 Hunter/Bybit secrets。

先以默认 `read_only=true` 检查账户和已有股票 Worker，凭据仅在 Runner 内使用；确认后输入最新 main SHA、Worker 是否已存在及 `read_only=false` 部署。部署工作流会检查源码、固定 Worker/DO/Cron 配置、账户 Worker 清单和预期存在状态；不符则停止。Wrangler 4.147.0 用 `--strict` 防止无提示覆盖远端配置，`--secrets-file` 将代码和 secret 一起部署，临时 secret 文件不输出并在退出时删除。此工作流没有 push 或 schedule 部署触发，也不通过手动 dispatch 股票流程制造自动周期证据。部署上传成功仍需下述实跑验收。

1. 在 Cloudflare 部署独立 `stock-shadow-scheduler` Worker，应用 `StockScheduler` 的 SQLite Durable Object 绑定 `STOCK_SCHEDULER` 和 v1 migration；Cron 为 `3,8,13,18,23,28,33,38,43,48,53,58 * * * *`。
2. 通过 Cloudflare Secret 设置 `GITHUB_ACTIONS_TOKEN`，不要放进代码、仓库、URL、日志或聊天。Token 只选择这个仓库，最少需要 Actions 写入和读取仓库内容权限；不需要交易权限或其他仓库权限。创建/扩大凭据权限须在操作前获得确认。
3. 执行 `node --test worker.test.mjs`。仓库独立 Actions 验证工作流会运行相同测试。
4. 验证实际 Cron 自动生成 GitHub `workflow_dispatch` 运行，运行成功且健康文件写回；至少连续观察 3 个 5 分钟周期，检查 `/health`。
5. 受控验证：缺失 Token / GitHub 429 / 超时 / 已有运行 / 并发 tick / 日历异常 / 周末 / 提前休市 / DST。测试不得删改真实前向持仓或账本。

## 告警和验收限制

`/health` 与 Workers 错误日志提供失联信号；尚未连接主动消息推送渠道。不能把“有健康接口”描述为“用户会自动收到即时告警”。另一个原生 GitHub cron watchdog 无法解决 GitHub cron 整体停发的问题。

部署完成之前只能称代码和测试完成；外部 Token、Durable Object 绑定、Cron 及连续实跑全部确认后才能称独立调度接通。Token 过期/撤销会显示错误并停止派发；应在到期前轮换。

2026-10-06 首次部署成功，但随后的 18 分钟只读验收未记录到自动心跳。随后改用与原表达式完全相同的显式分钟列表重新发布，属于配置恢复尝试，不据此认定原表达式是根因；须继续用自动周期和持久化证据验收。

## 2026-10-06 验收证据加固（需部署后生效）

- scheduled handler 在调用 DO 前记录 `SCHEDULED_EVENT_RECEIVED`，并把 Cloudflare `event.scheduledTime` 和 cron 传入私有 DO 路径；健康记录带 `trigger_source=scheduled`、`scheduled_at`、`cron`。无来源的历史记录或直接诊断 tick 不能自动算作真实 cron。
- GitHub 客户端将 fetch 作为独立函数调用，避免把 GitHub 实例当作 Workers 全局 fetch 的接收者。此修复消除运行时兼容隐患，不据此断言它解释了既有 `NOT_STARTED`。
- 只读验收分别判断真实触发、决策、唯一成功工作流、同 main 快照中的 run/source 持久化绑定、完整行情刷新。三次 ERROR、三次无派发心跳、手动探针、失败/重复运行或未绑定的持久化都不能给出完整通过。
- `PARTIAL` 行情与调度执行分开报告：可以证明执行和写回，同时保留行情不完整及整体验收未完成。源码 checkout 与创建 run 的 SHA 不一致时标为未验证，不能误报写回完成。
- 目前 dispatch API 没有返回 run ID，90 秒窗口唯一匹配仍只是明确标记的相关性；若同时有人手动触发相同股票工作流，则必须停止把该窗口当作自动端到端证据。
- 本轮修改不改变 cron、V3 策略、费用、资金、正式历史账本或 Hunter/Bybit 资源；更新部署与真实连续验收需要单独确认。
