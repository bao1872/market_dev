# Deployment Runtime Map

## 1. Compose 服务

| 服务 | 容器 | 端口 | 说明 |
|---|---|---|---|
| postgres | trading-postgres | 内网 | PostgreSQL 16，volume `trading-postgresdata` |
| redis | trading-redis | 内网 | Redis 7，volume `trading-redisdata` |
| backend | trading-backend | 8000 | FastAPI |
| frontend | trading-frontend | 80 | Nginx + React build |
| worker-bars-scheduler | trading-worker-bars-scheduler | 无 | 行情调度（16:00 bars_refresh，AsyncIOScheduler；板块同步已迁移至 `worker-after-close` 的 `syncing_boards` 步骤，CHANGE-20260716-007） |
| worker-strategy-scheduler | trading-worker-strategy-scheduler | 无 | DSA 调度 |
| worker-calendar | trading-worker-calendar | 无 | 交易日历 |
| worker-monitor | trading-worker-monitor | 无 | 盘中监控 |
| worker-strategy-batch | trading-worker-strategy-batch | 无 | 策略批处理 |
| worker-outbox | trading-worker-outbox | 无 | Outbox 扩张 |
| worker-delivery | trading-worker-delivery | 无 | 投递 |
| worker-after-close | trading-worker-after-close | 无 | 盘后编排 |
| worker-watchdog | trading-worker-watchdog | 无 | 恢复看门狗（清理 stale job/heartbeat） |
| worker-capture | trading-worker-capture | 8001 | 截图服务，volume `trading-capture-static` |

## 2. 关键环境变量

| 变量 | 用途 |
|---|---|
| DATABASE_URL | 后端与 Worker DB 连接 |
| REDIS_URL | Redis 连接 |
| JWT_SECRET | JWT 签名 |
| GIT_SHA | 镜像/运行版本 |
| BUILD_TIME | 构建时间 |
| FRONTEND_BASE_URL | 后端/worker 访问前端 |
| CAPTURE_WORKER_URL | 后端访问 capture worker |
| WORKER_TYPE | 区分 worker 行为 |
| CONFIG_FILE | 生产配置 |
| POSTGRES_USER/PASSWORD/DB | PostgreSQL 容器 |
| STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS | worker-strategy-batch run 级总超时（默认 7200） |
| BOARD_SYNC_ENABLED | `after_close_orchestrator` 的 `syncing_boards` 步骤开关（默认 `false`，CHANGE-20260716-007 改为 pywencai 语义；PR #77：`config.py` `_resolve_board_sync_enabled()` 优先级「环境变量 > CONFIG_FILE > 默认 False」，`docker-compose.prod.yml` worker-after-close 注入 `BOARD_SYNC_ENABLED: ${BOARD_SYNC_ENABLED:-false}`）；环境变量接受 `1`/`true`/`yes`/`on`（大小写不敏感）为真；`false` 时 `syncing_boards` 步骤跳过执行并记录 `status=skipped` + `reason_code=board_sync_disabled`；`true` 时通过 pywencai 拉取板块目录与成分股关系（`wencai_board_provider.WencaiBoardProvider`，需配置 `WENCAI_COOKIE`，容器需安装 Node.js —— pywencai `get_token()` 需 `subprocess.run(['node', ...])` 执行 `hexin-v.bundle.js`）；生产部署时设为 `true` |

Secret 不写入文档示例，不回显日志。

## 3. 生产只读检查命令

```bash
docker compose -f docker-compose.prod.yml ps

docker compose -f docker-compose.prod.yml logs --tail=100 worker-outbox

docker compose -f docker-compose.prod.yml logs --tail=100 worker-delivery

docker compose -f docker-compose.prod.yml logs --tail=100 worker-capture
```

数据库只读检查：

```sql
SELECT status, COUNT(*) FROM outbox GROUP BY status;
SELECT status, COUNT(*) FROM message_deliveries GROUP BY status;
SELECT worker_name, status, heartbeat_at FROM worker_heartbeats ORDER BY heartbeat_at DESC LIMIT 20;
SELECT job_name, status, business_date, started_at, finished_at FROM scheduler_job_runs ORDER BY created_at DESC LIMIT 10;
```

## 4. 部署验收

- backend health/readiness/version；
- frontend 200；
- Alembic single head；
- all worker heartbeat fresh and same Git SHA；
- latest market data as_of；
- DSA published run complete；
- monitor eligibility works；
- outbox processed；
- card/image delivery state queryable；
- capture static accessible；
- expired member 403 verified；
- admin jobs page shows real state.

## 4.0 AFC V1 部署状态（CHANGE-20260716-003 Known Gap）

- **backend/frontend 为本轮早期部署验证**：`docker compose up -d --no-deps backend frontend` 仅重建并重启 backend 与 frontend 两个服务，**不重启 worker**（`worker-*` 仍运行旧镜像、旧 Git SHA）。
- **worker 仍为旧镜像**：当前生产 worker 尚未升级，不会在盘后 `feature_snapshot_service.build_summary_payload` 中持久化新 `summary_payload.atomic_fact_contract_v1` 字段。
- **当前页面主要依赖旧快照 fallback**：Context API（persisted-first）读不到新持久化 payload 时，回退到同一纯函数 `compute_atomic_facts` 从 `structural_payload`/`temporal_payload` 重算（不回写旧快照），页面功能正常。
- **新 summary 持久化链路未在生产 worker 验证**：属于明确的早期验证决策（非错误），待 worker 镜像升级后在 production 验证新快照写入 + persisted-first 直读。
- 部署验收时 `worker_heartbeats.build_sha` 与 backend/frontend 的 `/version` SHA **允许不一致**（worker 旧镜像），这是预期状态，不得据此判部署失败。

## 4.1 `CORE_ONLY` 构建范围

`deploy.sh` 中 `CORE_ONLY=1` 用于受控恢复，只构建 `backend frontend`（不构建 `worker-capture`）。需要完整业务能力时必须运行对应 worker：趋势选股需要 strategy_batch/scheduler，飞书图片需要 capture/outbox/delivery。非 `CORE_ONLY` 模式下 deploy.sh 构建全部服务（含 `worker-capture`）。

## 5. Docker 镜像保护

- `node:20-alpine` 是受保护基础镜像，拉取很慢，禁止主动删除；
- 禁止 `docker image prune -a`；
- 除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`；
- 普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`；
- 测试期部署默认不备份数据库，禁止 `pg_dump`，禁止写入 `/root/backups` 或 `/root/web_dev/backups`。
