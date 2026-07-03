# Source Snapshot Used for v2

本候选包基于以下仓库事实整理：

- main merge commit: `40dd2287f0962910d2e272c468b3e5054abddaaf`
- 原 current docs implementation baseline: `ddca659b8c9d64b6a414da0b4bbd6f80f704aef1`
- `docs/README.md` 旧结构列出 current 00-18 共 19 个文件；
- `backend/app/main.py` 是 FastAPI 应用入口，include routers 覆盖 auth/me/instruments/calendar/market/bars/capture/indicators/strategies/strategy_runs/monitor_states/strategy_events/notifications/admin/watchlist/stock_detail_feishu/public_beta/plans/metrics；
- `frontend/src/App.tsx` 是前端路由事实源；
- `docker-compose.prod.yml` 是生产服务事实源；
- `docs/current/11-jobs-integrations.md` 确认 Feishu Platform App only；
- `docs/current/18-code-doc-alignment.md` 仍有生产 E2E、服务健康、历史质量债务等 gap；
- 生产只读审计发现 worker_heartbeats stale/running 僵尸记录。

本文件仅说明 v2 生成依据，不是当前设计事实源。
