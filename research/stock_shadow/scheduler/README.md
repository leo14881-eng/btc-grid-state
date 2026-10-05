# 股票独立调度与失联恢复

本模块只负责基础设施。V3 BUY / ADD / SELL、手续费、资金规则和交易日历 Gate 不变；仅影子模拟，不接触 Alpaca 密钥或真实订单。

## 已确认的问题与证据边界

2026-10-05 18:04—18:57 UTC，股票监控没有自动新运行。工作流启用、默认分支 main、Actions 权限正常，且没有排队任务；同仓库其他定时工作流也出现大间隔。手动运行 37359843404 用时 29 秒，112 项股票测试、26 项 P0 验收通过，327 个持仓更新、4 笔影子 SELL 并写回。

能够确认缺口在 GitHub 定时触发环节；无法从公开 API 证明其内部丢失/延迟的具体原因。GitHub 官方说明 schedule 可能延迟或丢弃：https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule 。单次 push 或手动成功不能证明连续调度正常。

## 工作方式

- Cloudflare Cron 每 5 分钟（UTC 03、08……58）唤醒独立 Worker，原 GitHub cron 保留备用。
- 固定仓库 `leo14881-eng/btc-grid-state`，固定 main，只允许触发 `stock-shadow.yml` 和 `stock-shadow-position-monitor.yml`。
- 读取同一 Git commit 的交易日历缓存、主扫描摘要、监控健康记录。报价失败、缺失股票或部分刷新不计为成功心跳。
- 开市监控超过 10 分钟无完整成功刷新，健康状态标记 stale；优先补跑监控。主扫描满 60 分钟需要刷新，且监控未严重失联时允许主扫描执行。
- 周末不补跑持仓监控；主扫描仍可进行研究。节假日/提前休市使用 Alpaca 日历缓存；日历未知时可以触发现有引擎查询日历，实际交易授权始终由引擎最后一道 Gate 决定。
- 检查 queued / in_progress / waiting / pending / requested 的任务，存在股票主扫描或监控时不重复触发。库存超过分页可验证范围时停止补跑，不猜测“没有任务”。
- Durable Object 事务保存 5 分钟派发冷却，保护并发唤醒和进程重启；POST 超时也保留冷却，避免立即重复提交。GitHub 内置 cron 与外部调度仍存在 API 可见性窗口，最终账本写入继续由共同 concurrency 和持久化版本检查保护。
- `/health` 只读，心跳过期、API 错误、开市监控 stale 返回 HTTP 503。公开 HTTP 请求不能触发补跑。健康状态及最近 32 条审计记录持久化；Workers 日志记录结果，不输出 Token 或 GitHub 响应正文。
- 调度器自身不是实时交易引擎；它仍依赖 GitHub Runner 和原有延迟行情。Cloudflare 调度也不是绝对准点保证。

## 部署（独立 Worker，不修改 Bybit 行情 Worker）

配置文件：`wrangler.jsonc`；入口：`worker.mjs`。

1. 在 Cloudflare 部署独立 `stock-shadow-scheduler` Worker，应用 `StockScheduler` 的 SQLite Durable Object 绑定 `STOCK_SCHEDULER` 和 v1 migration；Cron 为 `3-58/5 * * * *`。
2. 通过 Cloudflare Secret 设置 `GITHUB_ACTIONS_TOKEN`，不要放进代码、仓库、URL、日志或聊天。Token 只选择这个仓库，最少需要 Actions 写入和读取仓库内容权限；不需要交易权限或其他仓库权限。创建/扩大凭据权限须在操作前获得确认。
3. 执行 `node --test worker.test.mjs`。仓库独立 Actions 验证工作流会运行相同测试。
4. 验证实际 Cron 自动生成 GitHub `workflow_dispatch` 运行，运行成功且健康文件写回；至少连续观察 3 个 5 分钟周期，检查 `/health`。
5. 受控验证：缺失 Token / GitHub 429 / 超时 / 已有运行 / 并发 tick / 日历异常 / 周末 / 提前休市 / DST。测试不得删改真实前向持仓或账本。

## 告警和验收限制

`/health` 与 Workers 错误日志提供失联信号；尚未连接主动消息推送渠道。不能把“有健康接口”描述为“用户会自动收到即时告警”。另一个原生 GitHub cron watchdog 无法解决 GitHub cron 整体停发的问题。

部署完成之前只能称代码和测试完成；外部 Token、Durable Object 绑定、Cron 及连续实跑全部确认后才能称独立调度接通。Token 过期/撤销会显示错误并停止派发；应在到期前轮换。
