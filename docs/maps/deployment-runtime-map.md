# Deployment Runtime Map

## 1. Compose 服务

| 服务 | 容器 | 端口 | 说明 |
|---|---|---|---|
| postgres | trading-postgres | 内网 | PostgreSQL 16，volume `trading-postgresdata` |
| redis | trading-redis | 内网 | Redis 7，volume `trading-redisdata` |
| backend | trading-backend | 8000 | FastAPI |
| frontend | trading-frontend | 80 | Nginx + React build |
| worker-bars-scheduler | trading-worker-bars-scheduler | 无 | 行情调度 |
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

## 5. Docker 镜像保护

- `node:20-alpine` 是受保护基础镜像，拉取很慢，禁止主动删除；
- 禁止 `docker image prune -a`；
- 除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`；
- 普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`；
- 测试期部署默认不备份数据库，禁止 `pg_dump`，禁止写入 `/root/backups` 或 `/root/web_dev/backups`。
