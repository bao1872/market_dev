# 01 系统架构

## 1. 总体架构

```text
React Browser
  → Nginx Frontend
  → FastAPI Backend
  → PostgreSQL / Redis

Python Workers:
  bars_scheduler
  strategy_scheduler
  calendar_scheduler
  monitor_scheduler
  strategy_batch
  outbox
  delivery
  after_close_orchestrator
  capture service
```

PostgreSQL 是正式业务状态来源。Redis 只保存可重建缓存、锁和短期协调状态。

## 2. 代码主入口

| 职责 | 文件 |
|---|---|
| FastAPI 应用 | `backend/app/main.py` |
| 统一 Worker 入口 | `backend/app/worker.py` |
| 前端路由 | `frontend/src/App.tsx` |
| 生产编排 | `docker-compose.prod.yml` |
| 指标根数与契约 | `backend/app/constants/indicator_contract.py` |
| 策略资产 | `backend/app/strategy_assets/manifests/` |
| 权限上下文 | `backend/app/services/access_control_service.py` |
| Worker 用户资格 | `backend/app/services/eligible_user_service.py` |

## 3. 后端依赖方向

```text
API / Worker Orchestrator
        ↓
Application / Domain Service
        ↓
Repository / Strategy Runtime / External Adapter
        ↓
PostgreSQL / Redis / External Service
```

- API 负责认证、权限依赖、参数校验、响应，不复制业务规则；
- Service 负责业务状态、事务、资格、幂等和编排；
- Repository 负责数据库访问，不判断订阅和产品语义；
- Strategy Runtime 负责行情输入和指标计算，不决定用户权限；
- Adapter 负责 Pytdx、Mootdx、飞书、Redis、截图浏览器等外部系统。

## 4. 模块边界

| 模块 | 边界 |
|---|---|
| access | 用户、角色、订阅、Plan、资格、配额 |
| market_data | 行情、交易日历、聚合、数据新鲜度 |
| screening | DSA selector、StrategyRun、发布批次 |
| watchlist | 用户自选和额度 |
| monitoring | 完成 Bar 评估、状态、事件 |
| notifications | NotificationMessage、Outbox、Delivery、渠道 |
| capture | Capture Token、截图 worker、图片 URL |
| jobs | SchedulerJobRun、worker heartbeat、任务恢复 |
| admin | 管理 API、审计、运维页面 |
| indicator | 全局技术指标（SQZMOM_LB、SMC）纯函数计算；位于 `backend/app/strategy_assets/algorithms/features/`，不是 Service；SMC 按需启用（`include_smc=False` 默认），不进入 DSA/Node/Capture/监控/选股；FVG 完全排除（不计算、不返回、不缓存、不渲染） |

## 5. 端到端链路

### 盘后趋势选股

```text
bars_scheduler
→ 行情更新与覆盖率
→ queued StrategyRun
→ strategy_batch
→ DSA Runtime
→ StrategyResult
→ 完整性门禁
→ published_run_id
→ /screener 查询
```

### 盘中监控与通知

```text
user_watchlist_items
→ eligible_user_service
→ monitor_scheduler
→ 最新两根 completed 1m Bar
→ MonitorEvaluation
→ StrategyEvent
→ EventRecipient
→ Outbox
→ MessageDelivery
→ Feishu / message center
```

### 个股详情截图分享

```text
用户/管理员触发分享
→ stock_detail_feishu_service
→ 文字 NotificationMessage + Outbox
→ create_capture_token
→ worker-capture 访问 /capture/stock/:symbol
→ Capture Snapshot API
→ 图片 NotificationMessage + Outbox
→ Outbox Relay
→ Delivery Worker
→ Feishu Platform App
```

## 6. 实验隔离

实验可以共享只读基础行情，但必须隔离策略版本、结果标识、运行键、结果表或 schema。实验不得覆盖正式 published results、生产用户数据，也不得与生产 Worker 争抢任务。
