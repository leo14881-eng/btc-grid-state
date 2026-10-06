# Hunter 双 Venue Fast Watch / REST Active Fallback Phase 1

状态：候选实现，OBSERVATION_ONLY。未经服务器实测与至少24小时配对观察，不可称完成。

## 边界

现有小时Discovery/Research/Capital Review及5分钟Position Monitor原样保留。
新常驻服务只读取固定main snapshot及公共行情，写自身本地runtime文件。
它不能调用订单接口、写GitHub、修改portfolio或产生正式交易事件。
不存在第二正式writer。因此当前阶段的ARM、保护与EXIT是counterfactual
观察副本状态，不能描述为正式V2已ARM/已SELL。

正式Fast-review writer admission/IPC未启用。将来启用前需独立证明同锁/CAS、
full-quantity深度、generation、幂等、读回与通知一致性；本PR不解锁正式SELL。

资本仍20K，普通17K/战略3K。Tail4K仅stress diagnostic。
BUY门槛、V1、Profit Protection参数、Loss Recovery及任何BTC策略均未改。

## Venue身份必须有证据

每个position需execution_venue（BINANCE_SPOT/BYBIT_SPOT）、market_symbol、
market_type=spot。Bybit执行估算还需明确execution_fee_bps模型；缺失标UNKNOWN。
程序不从asset或Bybit可用性标签猜执行Venue。

旧position可能缺上述三字段。execution_channel是bybit_channel()可用性输出，
不是实际执行身份。只读前向model admission需同一main snapshot内匹配：
portfolio active generation、Monitor generation（或Hourly Research scan/liquidity generation）、
原始Binance Spot depth receipt的symbol/market、持仓last_exit_estimate的receipt时间、
完整quantity/capital/VWAP/net PnL重算及既有fee模型。Research还须reference_venue与
明确pair/base相符。身份回执限600秒，不能被当作Fast Review的新报价。
全部匹配才在副本赋CURRENT_SHADOW_EXECUTION_MODEL，保留source SHA、generation、
source_kind、raw book hash；historical_entry_execution_venue仍UNKNOWN。
不改正式portfolio，不把Bybit可用性当成交Venue，不覆盖已有/部分显式身份。
证据不匹配则PRIMARY_VENUE_IDENTITY_MISSING，缺证据不能假称保护覆盖。
实测main b75e7f23890a5843835a9395d42029bdaf159895：10个现有open positions
均凭匹配Monitor回执进入Binance当前模型观察路由；不是历史成交或真实行情运行验收。

可选verified_spot_markets必须含venue、symbol、market_type、base_asset、quote_asset、
verified=true。仅匹配仓位资产/USDT的验证记录允许secondary订阅。
secondary不参与primary execution，也不平均价格。

## 数据与运行方式

Binance公共market-only WS：wss://data-stream.binance.vision/ws。
bookTicker保留update id、received_at；没有E时event_time=null / RECEIPT_BOUND。
aggTrade独立保留E/a，不给book借用timestamp，不掩盖book停止更新。
Bybit公共Spot WS：wss://stream.bybit.com/v5/public/spot。
Spot tickers文档没有best bid/ask字段，因此用orderbook.1快照，无完整本地depth。
Bybit L1 idle快照可复用u，但ts必须递增；保留原u/seq与ts，不伪造exchange sequence。

两套连接、fallback budget、circuit、订阅/排序/health独立。
配置集中在research/hunter_fast_watch.py的Config：12秒stale probe、7秒fallback、
3并发、4秒timeout、5秒trigger/depth freshness、3个fresh恢复事件、23h50m轮换。
断线立即probe；TCP仍连接但单symbol无book更新同样probe。
REST与WS运行在独立async tasks，WS reconnect/backoff不阻塞REST。
REST仅当前持仓、验证secondary及BTC，绝不扩至Discovery universe。
Binance官方exchangeInfo动态读取weight上限，本地预算取300/min与官方5%的较小值；
响应used-weight监控共享IP压力，80%时暂停，不声称已独占全服务器IP预算。
Bybit采用保守300请求/min、3并发，403/429冷却600秒。

深度不足以覆盖完整模型quantity或fee未知则拒绝review。Binance与Bybit同Venue检查；
不同Venue不能替代primary。protect()/既有参数用于独立副本，负净收益gap记录原incident，
不创造盈利SELL。ADD改变cashflow fingerprint时丢弃在途旧review。

普通tick仅memory；runtime JSON原子写间隔5秒、单进程flock。
history限2000条，计数累计；每60秒另追加每日gzip JSONL观察档案（UTC日轮换、fsync），
保留完整runtime snapshot及source SHA，重启不会覆盖先前记录。档案不自动删除，需运维
监控磁盘与保留政策。档案仍是分钟快照，不是完整tick tape；不得据此虚构逐tick成交路径。重启恢复fresh本地counterfactual history；
报价、connected、pending不恢复。旧runtime超过60秒丢弃，重新fresh观察，不追认历史窗口。

订阅每30秒从最新main重新对账，因而包含5分钟Monitor的新snapshot；没有改动Monitor writer。
Watchdog健康报告增加fast_watch_health，Fast Watch故障不改原Hunter job成功状态。
缺部署、stale文件、fallback健康分别显示。无Fast Watch文件不阻断原Monitor。

## 部署候选（本轮未在服务器执行）

deploy/systemd/hunter-market-stream.service采用独立hunter-fast-watch用户、只读代码目录、
仅/var/lib/hunter-fast-watch可写、NoNewPrivileges、Restart=on-failure。
安装需要现有服务器运维write能力：创建专用用户；代码固定到本PR验收SHA；
安装deploy/hunter-fast-watch-requirements.txt到独立venv；为readback clone提供只读GitHub权限；
准备/var/lib/hunter-fast-watch/readback；安装unit并daemon-reload/start。
禁止复制真实交易凭据或给服务root写权限。

命令：python -m scripts.hunter_market_stream --repo <独立只读readback clone>
 --state /var/lib/hunter-fast-watch/state.json。
--duration仅用于有限smoke；--watchdog只读本地health。
实际systemd必须回读unit、PID、source SHA、两Venue连接/事件及fallback接管。
至少观察两个原Monitor自然5分钟周期、完整小时链继续正常、real_order_count=0。
当前MCP只读，不能安装/启动unit；不声明部署成功。

## A/B与验收

ab按shadow_id记录ws/monitor arm、peak、exit review及理论净利润；source SHA固定到main读回。
未出现的pair与执行成本保持null，不写0。delta=monitor时间-ws时间。
A/B只统计各position/cashflow窗口启动后的真实新观察；启动前ARM/holding peak仅作baseline，
不参与新检测延迟。Monitor sampled peak只累积后续MONITOR generation的last_price；
不把Research observation或历史回填peak冒充5分钟样本。ADD重置配对窗口并保留旧episode。
重启只恢复匹配cashflow的新版LIVE_WINDOW_ONLY_V1观察记录；旧schema不追认。
WS uptime仅本次process观察窗口；exchange时间latency是clock-dependent估计，
Binance receipt-only book没有exchange latency证据。实时统计不能证明成交或实际利润改善。
missed-profit/capture delta仍须24小时以上配对证据裁定，当前UNKNOWN。

自动测试覆盖路由、断线、重连、轮换、真实本地WS ping/pong、单symbol断流、
重复/旧/未来/错误事件、订阅增减、fallback、恢复验证、冲突、ARM/runner/giveback/gap、
深度不足、混合Venue拒绝、Bybit未知fee、cashflow变化在途拒绝、重启、并发隔离及无正式writer。
这些是测试，不是服务器runtime。

官方参考：
- https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md
- https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
- https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- https://bybit-exchange.github.io/docs/v5/ws/connect
- https://bybit-exchange.github.io/docs/v5/market/orderbook

未验收事项：历史成交身份（UNKNOWN）、服务器部署、两Venue官方真实连接/断流接管、完整24h A/B、
Fast review到正式Single Writer的future admission、真实ARM/EXIT样本。
