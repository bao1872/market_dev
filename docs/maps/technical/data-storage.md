# 数据存储 Map

核验状态：已基于本地原生启动核验（第一阶段）
最后核验日期：2026-07-26
核验提交：06bf5109b07a966207e7203e2b2ba12c7e12388d
事实所有权：PostgreSQL、Redis、表、Key、数据层级和权威存储

## 1. PostgreSQL

| Schema/表 | 责任 | 主键/唯一约束 | 写入者 | 读取者 | 可重算 |
|---|---|---|---|---|---|
| `alembic_version` | Migration 版本记录 | - | Alembic | Backend / 运维 | 否 |
| `instruments` | 股票主数据 | symbol 唯一 | 同步任务 | API / Worker | 否（外部权威） |
| `trading_calendar` | 交易日历 | date 唯一 | 启动时日历种子 / 同步任务 | API / Worker | 可重新同步 |
| `strategies` / `strategy_versions` | 策略目录与版本 | key / version 唯一 | 策略种子 / 管理员 | API / Worker | 可重新同步 |
| `selection_plan_members` / `monitoring_plans` / `monitoring_plan_states` | 选股计划与监控状态 | 复合主键 | Worker / API | API / Worker | 部分可重算 |
| `user_watchlist_items` | 用户自选 | user_id + symbol 唯一 | API | API | 否 |
| `worker_heartbeats` | Worker 心跳 | worker_name + instance_id | Worker | API / 看门狗 | 可清理 |
| `capture_jobs` | 截图任务 | id | Worker / API | API | 可重算 |
| `bars_15min` 等 bars 表 | 行情 K 线 | symbol + datetime 复合 | Worker / 同步任务 | API / Worker | 可重新拉取 |
| `composite_monitor_events` 等事件表 | 监控事件 | id | Worker | API | 可重算 |

> 表列表来自 `SELECT tablename FROM pg_tables WHERE schemaname='public' LIMIT 10`，仅展示前 10 张；完整 Schema 待后续核验。

## 2. 数据分层

| 层级 | 实际表或位置 | 删除保护 |
|---|---|---|
| 原始/外部 | `instruments`、`bars_*` | 核心保护 |
| 标准化 | `trading_calendar` | 核心保护 |
| 复权/转换 | bars 表内复权字段（待核验） | 需明确 |
| 因子/特征 | `strategy_assets` 计算结果、特征快照表（待核验） | 可重算 |
| 事件 | `composite_monitor_events` 等 | 可重算 |
| run/发布状态 | `monitoring_plans`、`monitoring_plan_states`、after-close run 表（待核验） | 关键状态 |
| 临时任务 | `capture_jobs`、`worker_heartbeats`、`scheduler_job_runs` | 可清理但需范围 |

## 3. Redis

| 逻辑 DB | 运行位置 | Key 类别 | TTL | 写入者 | 读取者 |
|---|---|---|---|---|---|
| DB 15 | 本地 | 本地健康检查键、潜在本地队列/锁/缓存 | 短 TTL / 待明确 | 本地进程 | 本地进程 |
| DB 0 | 远程 | 远程队列、锁、缓存 | 待核验 | 远程 Worker / Scheduler | 远程 Worker / Scheduler |

## 4. Key 与队列

| Key/队列模式 | 责任 | 环境隔离 | 清理方式 |
|---|---|---|---|
| `panji:local:health:*` | 本地 Redis 连接健康检查 | DB 15 本地专用 | 立即删除 |
| 远程队列/锁/缓存模式 | 待核验 | DB 0 远程专用 | 待核验 |

## 5. 权威事实源

- 行情：外部数据源（pytdx / qstock / mootdx）+ `bars_*` 表；
- 股票基础信息：`instruments` 表；
- 行业/概念：待核验；
- 因子结果：策略资产计算或特征快照表；
- 事件：`composite_monitor_events` 等；
- run 状态：`monitoring_plans` / `monitoring_plan_states` / after-close run 表；
- published_run_id：待核验；
- 用户权限：`users` / `roles` / `subscriptions`；
- 自选：`user_watchlist_items`；
- 盘中临时状态：Redis（待核验）。

## 6. 高风险点

- 本地与远程共享 PostgreSQL，本地开发中的 DELETE/UPDATE 可能影响远程数据；
- `backend/app/config.py` 中 Redis URL 默认回退到 `redis://localhost:6379/0`，若本地 `.env` 缺失可能进入远程 DB 0；
- 本地 `docker-compose.yml` 仍保留 redis 服务，存在误导风险；
- 后端 lifespan 自动执行策略种子和日历刷新，启动即写入 PostgreSQL；
- 当前未做本地只读权限限制。
