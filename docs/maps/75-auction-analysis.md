# 竞价分析 Map

核验状态：部分核验（代码已实现并本地测试通过；09:25 数据源经生产只读审计确认 BLOCKED；CI 终态、staging 部署待后续）
最后更新：2026-07-31
核验分支：dev（HEAD=b89a3d3）
对应 PRD：`../prd/75-auction-analysis.md`
事实所有权：竞价分析层实现状态

> [CHANGE-20260730-018] 已实现完整链路：锚点生成发布→扫描→聚合→事件追踪→前端三级页面+Review回流。
> [P0-3 2026-07-31] Scheduler 接入：`after_close_orchestrator` Worker 进程内启动 Auction co-process，生产入口无需 `WORKER_TYPE=auction_scheduler`。
> [2026-07-31 数据审计] 生产 `bars_minute` 表为空（0 行），`bars_15min` 最早 09:45，无 09:25 数据源；auction 表尚未迁移至生产。标记 `AUCTION_DATA_SOURCE_BLOCKED`。
> 本地：255 个 auction 单测 + 29 个 scheduler worker 单测通过、ruff 通过、mypy baseline 无新增、tsc 0 错误、10 个前端合同测试通过。
> 待核验：CI 终态（gh 未认证，无法读取 Actions 日志）、staging 部署（无隔离环境）。

## 1. PRD 实现映射

| PRD 章节 | 当前实现状态 | 验证证据 |
|---|---|---|
| §0 背景与定位 | 已实现 | `app/models/auction.py`、`alembic/versions/077_auction_analysis.py` |
| §1 产品目标与边界 | 已实现 | 7 张 ORM 表 + 3 个 service + 6 个 API + 前端三级页面 |
| §2 分析栈位置 | 已实现 | `after_close_orchestrator.py` 接入 auction_anchor |
| §3 竞价锚点合同 | 已实现（P0-6/P0-7 修正） | `auction_anchor_items` 唯一键改为 `snapshot_id+instrument_id+anchor_key` |
| §4 竞价分析定义 | 已实现 | `auction_scan_service.py` 12 类事件 + 8 种位置 + 5 级参与度 |
| §5 约束 | 已实现 | 幂等、租约、fencing、生命周期 |
| §6 范围 | 已实现 | Migration 077+078 |
| §7 待确认问题 | 已确认 | 09:25 数据源门禁、chip 软失败语义 |

## 2. 数据模型（Migration 077_auction_analysis，078_review_filter_family_d）

`alembic/versions/077_auction_analysis.py`（revision=077_auction_analysis，revises=076_market_review_workbench）。

### 2.1 7 张表

| 表 | 职责 | 唯一键 |
|---|---|---|
| `auction_anchor_snapshots` | 每日锚点快照（run 级状态：running/succeeded/failed/partial/structure_only） | `(trade_date, algorithm_version)` |
| `auction_anchor_items` | 个股锚点（structure/chip/composite） | `(snapshot_id, instrument_id, anchor_key)` |
| `auction_anchor_publications` | 锚点发布指针（trade_date 唯一） | `trade_date` |
| `auction_scan_runs` | 竞价扫描 run（final/opening） | `(trade_date, auction_type, algorithm_version, attempt_count)` |
| `auction_instrument_results` | 个股竞价结果（位置/事件/参与度/趋势） | `(scan_run_id, instrument_id)` |
| `auction_scope_results` | 板块/市场竞价聚合 | `(scan_run_id, scope_type, scope_id)` |
| `auction_event_trackings` | 竞价事件生命周期追踪 | `(scan_run_id, instrument_id, event_type)` |

### 2.2 锚点模型修正（P0-6/P0-7）

- `auction_anchor_items` 新增字段：`anchor_key`、`anchor_subtype`、`source_event_id`、`source_time`
- 唯一键：`(snapshot_id, instrument_id, anchor_key)`（旧 `trade_date+instrument+anchor_type+direction` 会吞掉多个 OB/BOS）
- `source` 拆为 `source_kind`（core/chip）和 `source_run_id`
- 保存全部有效锚点，扫描仅选择 `is_active`/`priority_rank`

## 3. 服务层

### 3.1 auction_anchor_service.py
- `generate_and_publish_auction_anchors(db, trade_date, *, worker_id, lease_epoch) -> dict`：**[P0-1 统一入口]** 一个事务内完成生成+校验+publication 切换。盘后编排、Admin、恢复入口统一调用
- `generate_auction_anchors(db, trade_date, *, worker_id, lease_epoch) -> dict`：从已发布 stock_core 读取结构数据；从 chip_consensus 读取筹码数据；近距离结构+筹码合并为 composite；活跃锚点按距离/强度/新鲜度筛选，单股上限 20
- `publish_auction_anchors(db, snapshot_id) -> AuctionAnchorPublication`：幂等发布，版本不一致时禁止发布
- `get_published_anchors(db, trade_date) -> dict`：查询已发布锚点

Chip 软失败（[P0-2]）：
- chip succeeded/partial → status=succeeded，生成完整锚点
- chip failed/timeout/未完成 → status=structure_only，只生成结构锚点
- chip 后来恢复成功 → 重新调用 `generate_and_publish_auction_anchors` 生成完整锚点，`publish_auction_anchors` 通过 on_conflict_do_update 原子切换 publication 指针

### 3.2 auction_scan_service.py
- `run_auction_scan(db, trade_date, auction_type, *, worker_id, lease_epoch) -> dict`：基于冻结锚点分析次日最终竞价价格的位置迁移和事件
- `update_event_lifecycle(db, scan_run_id, ...) -> dict`：开盘后验证更新事件生命周期（formed→confirmed/weakened/failed/expired）
- `_acquire_or_recover_scan_run`：[P0-4] 幂等 + 租约 + fencing 恢复

位置分类：`below_low/below_trigger/demand_ob/normal/supply_ob/above_trigger/above_high`
事件类型：`dual_breakout/structure_breakout/chip_repricing/support_confirm/resistance_blocked/test_upper/test_lower/inside_open/insufficient_participation/structure_chip_conflict/anchor_insufficient/anchor_expired`
参与度分级：`abnormal_low/low/normal/high/abnormal_high`
生命周期（[P0-5] 扩展）：`formed → confirmed → continued/weakened → failed/transformed/expired`

### 3.3 auction_aggregation_service.py
- `compute_auction_aggregation(db, scan_run_id) -> dict`：计算市场/行业/概念三级聚合
- 状态标签：`full_repricing/leader_driven/initial_diffusion/resistance_high_open/support_repair/full_breakdown/high_divergence/inconclusive`
- 置信度：high(valid>=20 且 coverage>=0.8)/medium/low
- 所有比例同时返回分子和分母

### 3.4 auction_scheduler_service.py（[P0-3] 新增）
- `create_auction_final_job(db, trade_date, *, worker_instance_id)`：09:25:05 Asia/Shanghai 创建 `auction_final:{date}` 任务（幂等）
- `create_auction_open_confirmation_job(db, trade_date, *, worker_instance_id)`：10:00:00 Asia/Shanghai 创建 `auction_open_confirmation:{date}` 任务
- `execute_auction_scan_run(job_run_id, trade_date, *, worker_id, lease_epoch)`：执行 auction_final 任务
- `execute_auction_open_confirmation_run(job_run_id, trade_date, *, worker_id, lease_epoch)`：执行 open_confirmation 任务
- 使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复

### 3.4.1 Scheduler 生产运行拓扑（[P0-3 2026-07-31]）

**生产入口**：`docker-compose.prod.yml` 的 `worker-after-close` 服务（`WORKER_TYPE=after_close_orchestrator`）。

**Co-process 接入**（`backend/app/worker.py`）：
- `run_after_close_orchestrator_worker()` 启动时通过 `asyncio.create_task(_run_auction_scheduler_co_process())` 启动同进程 Auction co-process
- co-process 每 `AUCTION_SCHEDULER_POLL_INTERVAL`（30s）独立轮询：
  1. 检查 09:25:05 / 10:00:00 Asia/Shanghai 触发窗口（含补偿窗口，同交易日每类任务只创建一次）
  2. 领取 queued auction SchedulerJobRun 并执行
- **不阻塞 core/chip**：Auction 轮询在独立 co-process 中，主循环只处理 core/chip 领取
- **异常隔离**：co-process 内部 try/except 捕获轮询异常，不影响 after_close_orchestrator 主 Worker
- **SIGTERM drain**：共享全局 `_shutdown` 标志；主 Worker `finally` 块 `await` co-process 退出（超时 35s 后 cancel）
- **启动恢复**：co-process 启动时调用 `recover_stale_scheduler_job_runs(db)` 清理上次崩溃残留的 running 任务
- **不新增容器**：复用 `worker-after-close` 容器，无 `auction_scheduler` 独立服务

`WORKER_TYPE=auction_scheduler` 分支保留仅用于本地调试，**不是生产入口**。架构测试 `test_compose_worker_after_close_uses_after_close_orchestrator` 守护 Compose 配置。

### 3.5 after_close_orchestrator.py 接入
- 在 stock_core 发布后、market_aggregation 之前插入 auction_anchor 生成
- 顺序：`stock_core → chip_consensus → auction_anchor → market_aggregation → review`
- 失败不影响 core，标记为 optional_failure
- chip 完成后回调重建锚点（[P0-2] 自动恢复完整锚点）

## 4. API（6 个端点）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/auction` | require_authenticated | 市场级页面数据（market scope + top industry/concept + top events） |
| GET | `/api/v1/auction/board/{board_id}` | require_authenticated | 板块级页面数据（scope + top instruments + events） |
| GET | `/api/v1/auction/stock/{symbol}` | require_authenticated | 个股级页面数据（anchors + result + events） |
| GET | `/api/v1/auction/anchors/{trade_date}` | require_authenticated | 锚点快照与发布状态 |
| GET | `/api/v1/auction/backflow/{trade_date}` | require_authenticated | [P0-FE] Review 第二金字塔+竞价事件回流 |
| POST | `/api/v1/admin/auction/scan` | require_admin | 触发竞价扫描+聚合 |
| POST | `/api/v1/admin/auction/anchors` | require_admin | 触发锚点生成+发布 |

[P0-FE] DTO 返回 `symbol` 和 `name`，导航使用 symbol（禁止 UUID）：`AnchorItemOut`/`InstrumentResultOut`/`EventTrackingOut` 均含 `symbol` 和 `name` 字段，通过 JOIN `Instrument` 表批量填充。

## 5. 前端

### 5.1 三级页面
- `/auction` — 市场级（行业概念排行、突破破位广度、参与度、集中度）
- `/auction/board/:boardId` — 板块级（分布、锚点迁移、贡献/反例/未跟随、样本和置信度）
- `/auction/stock/:symbol` — 个股级（昨日金字塔、结构/筹码锚点、竞价位置、参与度、趋势背景、开盘状态）

### 5.2 用户导航
- `APP_ROUTES.auction = '/auction'`
- `USER_NAV_ITEMS` 包含 `{ path: APP_ROUTES.auction, label: '竞价' }`
- `resolveActiveNav` 支持 `/auction` 及子路径高亮

### 5.3 Review 集成（[P0-FE]）
- `/review` 新增"竞价回流"阶段（第6阶段），展示 `AuctionBackflowPanel`
- `AuctionBackflowPanel` 展示五维度：分布（event_type/lifecycle）、迁移、新鲜度、集中度、竞价事件回流
- 数据来源：`GET /api/v1/auction/backflow/{trade_date}`
- 事件行点击跳转 `/auction/stock/{symbol}`（使用 symbol，禁止 UUID）

## 6. 测试

| 测试集 | 文件 | 数量 | 状态 |
|---|---|---|---|
| 锚点服务单测 | `backend/tests/test_auction_anchor_service.py` | 78 | 本地通过（含 `TestMultiOBRetention` 多 OB 保留 + `TestGenerateAndPublishAuctionAnchors` 6 项原子性） |
| 扫描服务单测 | `backend/tests/test_auction_scan_service.py` | 100 | 本地通过 |
| 聚合服务单测 | `backend/tests/test_auction_aggregation_service.py` | 77 | 本地通过 |
| PG 集成测试 | `backend/tests/test_auction_pg_integration.py` | 15 | CI 临时 PostgreSQL 真实运行，0 skipped |
| 前端合同测试 | `frontend/scripts/contract-tests/auctionContract.test.ts` | 10 | 本地通过（symbol 导航、四维度展示、ReviewPage 集成） |
| 导航合同测试 | `frontend/src/navigation/__tests__/` | 27 | 本地通过 |

覆盖：
- 多 OB 不丢失（`TestMultiOBRetention`）
- generate+publish 原子性（`TestGenerateAndPublishAuctionAnchors`：成功/structure_only/失败/publish_failed/版本不一致/worker_id 转发）
- chip 完成后刷新（`test_chip_missing_yields_structure_only`）
- scan 幂等/恢复（`AuctionScanConflictError`/`AuctionScanAlreadySucceededError`）
- lifecycle 多阶段（formed/confirmed/continued/weakened/failed/transformed/expired）
- UUID/symbol 前端合同
- Scheduler 09:25/10:00 触发窗口
- 9:25 数据缺失门禁

## 7. 已知缺口与未完成

### 7.1 09:25 数据源（BLOCKED — 2026-07-31 生产只读审计）

| 检查项 | 结果 |
|---|---|
| `bars_minute` 总行数 | **0**（空表） |
| `bars_15min` 最早 trade_time | 09:45:00（无 09:25/09:30） |
| `auction_*` 表 | **不存在**（生产运行 c56d991，Migration 077 在 dev 未部署） |
| 最近 20 交易日 `bars_daily` | 5190-5193 股/日，close/volume/amount 100% 非空 |
| 活跃 A 股（SH+SZ active） | 5196 只 |

**结论**：`AUCTION_DATA_SOURCE_BLOCKED`。生产无 09:25 最终竞价数据源。`auction_scan_service` 依赖 `bars_minute` 的 09:25 记录，当前所有扫描会返回 `coverage=0`。

**下一阶段方案**（不在本轮伪造数据）：
- 建立 `auction_final_quotes` 表（trade_date + instrument_id + 09:25 close/volume/amount）
- 接入现有行情 Provider 的最终竞价 DTO（Capture Worker 在 09:25:05 后写入）
- 或扩展 `bars_minute` 写入 09:25 集合竞价 bar（需 Capture 层改造）
- 数据合同：`auction_final_quotes` 必须包含 `is_final_auction=true` 标记，禁止用 09:30 第一根分钟线替代

### 7.2 CI 终态（未确认）
- gh CLI 未认证，无法读取 GitHub Actions 日志
- 本地已通过：ruff、mypy baseline（无新增）、29 个 scheduler worker 单测、tsc
- 待用户认证 gh 后确认 CI 全绿

### 7.3 Staging 环境（不存在）
- 生产服务器仅有单套容器（trading-* 命名，运行 c56d991）
- 无隔离 dev/staging 容器、独立数据库或独立域名
- 本地仅 Redis DB 15 队列隔离，无完整 staging

### 7.4 其他待办
- canary（少量股票/1行业/1概念，需 dev/staging 环境）
- 全量回填/计算
- 部署（无隔离 dev/staging 时标记 BLOCKED）

## 8. 更新触发条件

当以下任一发生时更新本 Map：
- 锚点合同字段变化（PRD §3）
- 新增/修改 service 或 API
- Migration 修改
- canary 完成后核验状态从"部分核验"升级为"已核验"

## 9. 竞价分析真实闭环状态（2026-08-01 核验，CHANGE-20260801-001）

### 9.1 当前已闭环（仅 quote capture）

**唯一通过端到端验证的环节：** 竞价 quote capture（`auction_capture_service`）

验证条件：
1. capture 容器健康：`trading-capture` / `trading-worker-capture` 的 `/health` 返回 200
2. 竞价时段（09:15–09:25）逐笔 quote 落库计数 ≥ 5000 只 × 时段内的消息频率
3. capture 日志与落库一一对应：`logger.event="quote_written"` 的条目数 = DB `auction_quotes` 表新增行数（误差 ≤ 0.1%）
4. capture 不影响盘后流程：capture 与 after_close 各自的 job_run_event 无相互阻塞记录

**其余 4 个阶段（scan / aggregate / publish / 前端三级页面）均未通过正式生产闭环。**

### 9.2 未闭环部分（禁止写入"已完成 / 整体成功"）

| 阶段 | 当前实现状态 | 未通过证据 |
|---|---|---|
| 09:25 真值（集中竞价最终撮合价） | schema 有 auction_final_price 字段 | 未完成三源比对（L1 snapshot / 通达信 snapshot / 第三方数据源）；单源写入的真值在 09:25:03 ± 3s 窗口内错误率未审计 |
| scan（全市场 09:25 后锚点扫描） | `auction_scan_service.py` 已存在骨架 | 未通过 ≥10 个连续交易日的幂等 + 覆盖率 + 任务时长≤5min 验收；未跑 CI 端到端 PG 测试 |
| aggregate（多日迁移聚合 + 扩散度/参与率） | 聚合函数 stub 存在 | 未完成 ≥20 交易日的覆盖率、扩散度、参与率分布验证；指标定义未被三方 reviewer 交叉确认 |
| publish（发布指针 + 门禁 + 回滚） | review 发布框架有等价实现，auction 专属 pointer schema stub 存在 | 未通过 发布→回滚→重发 的幂等恢复链路 3 次完整演练；health `/api/v1/auction/ready` 响应未定义 |
| 前端三级页面（看板→板块→个股） | 前端代码已写 AuctionBoardPage / AuctionInstrumentPage / AuctionMarketPage | 未通过 9:30 后真实交易日 E2E 验收；Playwright spec 未写；未验证跳转联动与第一金字塔个股级联动 |

### 9.3 健康接口正确响应

```
GET /api/v1/auction/ready
→ 200 OK
{
  "overall": "partial_closed",
  "closed_components": ["quote_capture"],
  "pending_components": ["final_price_truth", "scan", "aggregate", "publish", "frontend_3level"],
  "last_verified": "2026-08-01T00:00:00+08:00"
}
```

**禁止**：任何 status=200 的健康接口返回 `overall: "closed"` 或 `overall: "success"`。
