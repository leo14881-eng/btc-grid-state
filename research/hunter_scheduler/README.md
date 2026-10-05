# Hunter independent heartbeat

Infrastructure only. Cloudflare Cron dispatches the existing Hunter Position Monitor on main with trigger_source=cloudflare. It does not read or store portfolio/trade state and has no real-trading authority.

Deployment requires a Cloudflare secret named GITHUB_ACTIONS_TOKEN with repository-scoped Actions permission sufficient only to dispatch the workflow. Never commit the token.

The native GitHub */5 schedule remains enabled. Duplicate triggers are made safe by the authoritative 5-minute generation gate persisted on main.

Test: node --test worker.test.mjs
