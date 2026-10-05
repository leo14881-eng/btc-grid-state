# Hunter Vultr Position Monitor

仅影子模拟；V2 初始资金保持 20,000 USDT；不改 PR #10 或交易策略。

runner 保留 flock、Generation Guard、CAS、最多五次 push 尝试、每次重新 CAS 和远端回读。新增完整 portfolio/summary 时间戳与平仓数量校验、V2 初始资金约束、Leading Risk 权限校验，以及发布提交祖先关系和七份权威文件内容匹配。

每个周期使用独立临时 checkout，不 reset 主 checkout。generation 固定为 admission gate 返回值，跨时段完成也不会消费下一周期。远端权威校验成功后才输出 SERVER_POST_PUSH_READBACK_OK。

## 验证与部署

故障注入使用临时 bare remote；不得在生产 main 制造冲突。

运行 `python3 -m unittest discover -s tests -p 'test_hunter*.py'` 和新建的 Hunter Vultr Runner Infrastructure CI。PR 通过后合并，保留服务器全部原始备份；从合并后的 main 将 runner 安装到临时路径，原子 rename 为 /usr/local/bin/hunter-monitor-runner.sh。现有 systemd unit/timer 原样版本化，无需重启 timer。

部署后验证多个自动周期、持仓与交易记录连续性、资金及 shadow invariant。暂时保留 GitHub cron 和 Cloudflare fallback；调度角色切换、其他任务迁移及历史停机回放需独立验收。
