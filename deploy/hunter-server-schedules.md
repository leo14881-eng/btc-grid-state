# Hunter 调度与恢复说明（2026-11-08）

## 1. 当前可验证的 GitHub 备用调度

以下为仓库工作流文件中的 schedule 配置；它们是 GitHub Actions 触发计划，不代表服务器 systemd 定时器已健康运行。

| 作业 | GitHub Actions cron（UTC） | 用途 |
| --- | --- | --- |
| Hunter 5m Position Monitor | `*/5 * * * *` | 只管理现有影子仓位；不创建真实订单 |
| Hunter Stale-Result Watchdog | `*/10 * * * *` | 检查发现和持仓监控是否过期；过期后显式分发恢复工作流 |
| Hunter Multi-Venue Universe Scan | `17 * * * *` | Binance / 已验证 Bybit USDT 现货发现 |
| Hunter Blind Replay | `31 * * * *` | 历史盲回放 |
| Hunter Missed Opportunity Replay | `55 16 * * *` | 只读漏失机会回放 |
| Hunter Potential Research Pipeline | `workflow_run` | 发现工作流成功后启动研究闭环 |

## 2. 重要恢复机制

GitHub Actions 使用 `GITHUB_TOKEN` 推送的 commit 不会自动触发另一个 workflow。旧 Watchdog 只修改 trigger 文件并 push，不能保证触发 Monitor / Discovery；这会造成“Watchdog 成功但恢复工作流未启动”的假恢复。

2026-11-08 起，Watchdog 已改为：
- 每 10 分钟运行一次；
- 保留 trigger 文件作为审计痕迹；
- 通过具备 `actions: write` 权限的 `actions/github-script@v7` 与 GitHub REST `createWorkflowDispatch` 显式分发 `hunter-position-monitor.yml` 和 `hunter-cex-universe.yml`；
- 由定时/普通 push 启动的 Discovery 继续通过 `workflow_run` 启动 Research；Watchdog 手动分发的 Discovery 在完成后显式 `workflow_dispatch` Research，避免 GITHUB_TOKEN 事件链中断。Vultr 本地 runner 会跳过 GitHub-only `uses:` 分发步骤，仍使用本地 systemd 自愈。

## 3. 服务器主调度状态

Vultr 主运行证明文件目前过期，最后可验证的主运行证明停留在 2026-10-17 至 2026-10-18。服务器 systemd timer、Cloudflare 触发器及当前进程状态尚未取得新的只读运行证据，因此一律标记为 **UNVERIFIED**，不得把历史安装说明当作当前健康证明。

在服务器主运行证明恢复并满足新鲜度、主分支回读、影子权限和零真实订单条件前，GitHub Actions 备用路径保持启用。

## 4. 资金与交易边界

- `real_trading_enabled=false`
- `capital_authority=NONE_SHADOW_ONLY`
- Monitor、Discovery、Research、Replay 均为研究或影子流程，不得自动下单。
- 用户是唯一真实交易执行者。
- Hunter 新增真实山寨币 BUY/ADD 受 AXS 净盈利退出及 Hunter 独立盈利证据双重门槛约束。
- 本说明不授权修改任何资金上限、持仓或真实订单。
