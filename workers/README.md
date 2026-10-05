# Hunter Bybit Worker 部署与验收

整份替换 Cloudflare Worker 编辑器中的代码为 `hunter-bybit-proxy.js`，点击 Deploy。
保持现有 `HUNTER_PROXY_TOKEN` 环境变量，不修改 GitHub 或 Bybit Alpha 密钥。

接口：

* GET `/health`：应返回 `version: 2`。
* GET `/bybit/spot`：全量现货上架列表；GitHub 缓存 24 小时。
* GET `/bybit/spot?symbol=BTCUSDT`：保留单币调试兼容。
* GET `/bybit/tickers`：全量现货价格/24h 成交量；每轮重新采集。
* POST `/bybit/early-klines`：JSON `{"symbols":["BTCUSDT","FLUIDUSDT","CARVUSDT"],"end":<当前毫秒时间戳>}`。
  每批最多 20 个 USDT 交易对，每币 5 根 1h + 25 根 15m K 线。
  上游并发最多 4，45 秒总超时，最多 40 次上游请求。
* POST `/bybit/alpha/token-list`：原有鉴权与签名内容保持。

所有上游地址与 category 固定；无任意地址转发、无交易下单接口。
Alpha 与现货独立；现货列表不会被当作 Alpha 全量发现。

部署后执行只读路由检查，再验收发现流水线和下游 EARLY：

1. `/health` version=2，tickers retCode=0，K 线批量请求可读。
2. discovery venue_status.bybit.source=OFFICIAL_BYBIT_V5_VIA_WORKER。
3. worker snapshot 与本轮 discovery 时间和 SHA256 一致。
4. 若所有 Bybit 独有资产 K 线有效，才允许 bybit_signal_complete=true。
   新上架历史不足、短时请求失败等必须逐币列出，不能伪装为完整覆盖。
5. EARLY report.bybit_only_unscored 和 signal_count 与快照一致。
   数据完整也不保证 EARLY：仍需满足现有独立信号评分。

重合币沿用 Binance 的现有信号，独有币使用 Bybit BTC 基准与同交易所 K 线。
发现结果仍为 research_only；execution_supported=false，不能由这次接入放开真实交易或绕过 V2 交易准入。

GitHub Hosted Runner 无需部署常驻服务器或 WebSocket。完整缓存、快照和下游状态仍持久化到 main。
