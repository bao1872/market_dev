# 竞价分析 Map

核验状态：代码、PG Integration 与同一 SHA CI 已验证；生产双源仍阻断
最后更新：2026-08-01
核验分支：`codex/panji-full-closure-20260801`
对应 PRD：`../prd/75-auction-analysis.md`
事实所有权：竞价分析层实现状态

> [CHANGE-20260801-002] scan/aggregate/三级页面已有实现；本轮补齐独立来源真值门禁、
> 共识报价、专属 analysis pointer 和恢复幂等。当前仅有 mootdx/pytdx 同一供应链，
> 因此生产准确状态为 `blocked_external_auction_truth_source`。

## 1. PRD 实现映射

| PRD 章节 | 当前实现状态 | 验证证据 |
|---|---|---|
| §0 背景与定位 | 已实现 | `app/models/auction.py`、`alembic/versions/077_auction_analysis.py` |
| §1 产品目标与边界 | 已实现 | 7 张 ORM 表 + 3 个 service + 6 个 API + 前端三级页面 |
| §2 分析栈位置 | 已实现 | `after_close_orchestrator.py` 接入 auction_anchor |
| §3 竞价锚点合同 | 已实现（P0-6/P0-7 修正） | `auction_anchor_items` 唯一键改为 `snapshot_id+instrument_id+anchor_key` |
| §4 竞价分析定义 | 已实现 | `auction_scan_service.py` 12 类事件 + 8 种位置 + 5 级参与度 |
| §5 约束 | 已实现 | 幂等、租约、fencing、生命周期 |
| §6 真值合同 | 已实现，外部源阻断 | `auction_truth_service.py` |
| §7 编排与发布 | 已实现并通过 PG CI | Migration 082 + `auction_publication_service.py`；Run `30731828236` |

## 2. 数据模型（Migration 077、082）

`alembic/versions/077_auction_analysis.py`（revision=077_auction_analysis，revises=076_market_review_workbench）。

### 2.1 竞价核心表

| 表 | 职责 | 唯一键 |
|---|---|---|
| `auction_anchor_snapshots` | 每日锚点快照（run 级状态：running/succeeded/failed/partial/structure_only） | `(trade_date, algorithm_version)` |
| `auction_anchor_items` | 个股锚点（structure/chip/composite） | `(snapshot_id, instrument_id, anchor_key)` |
| `auction_anchor_publications` | 锚点发布指针（trade_date 唯一） | `trade_date` |
| `auction_scan_runs` | 竞价扫描 run（final/opening） | `(trade_date, auction_type, algorithm_version)` |
| `auction_instrument_results` | 个股竞价结果（位置/事件/参与度/趋势） | `(scan_run_id, instrument_id)` |
| `auction_scope_results` | 板块/市场竞价聚合 | `(scan_run_id, scope_type, scope_id)` |
| `auction_event_trackings` | 竞价事件生命周期追踪 | `(scan_run_id, instrument_id, event_type)` |
| `auction_quote_capture_runs` | 每来源及共识 capture run | `(trade_date, source, test_namespace)` |
| `auction_final_quotes` | 来源/共识最终报价及 raw evidence | `(trade_date, instrument_id, source, capture_run_id)` |
| `auction_analysis_publications` | 正式 scan+aggregate 可见性 pointer | `(trade_date, algorithm_version)` |

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

### 3.3A auction_truth_service.py / auction_publication_service.py

- `fetch_quote_sources` 并发调用 provider，并把 `source_id/provider_family` 固化到报价事实。
- `decide_auction_truth` 按 `provider_family` 去重；同一供应链不同 server 只计一个来源。
- `aggregate_auction_truth` 区分 `verified/conflict/partial/blocked_external`，只有 verified 生成
  `verified_consensus` 报价。
- `publish_auction_analysis` 校验 truth、namespace、capture、scan、coverage 和 aggregate，
  幂等写 `auction_analysis_publications`。
- 用户 API 的 `_get_latest_scan_run` JOIN 专属 pointer，不再直接暴露 succeeded/partial run。

### 3.4 auction_scheduler_service.py（[P0-3] 新增）
- `create_auction_final_job(db, trade_date, *, worker_instance_id)`：09:25:05 Asia/Shanghai 创建 `auction_final:{date}` 任务（幂等）
- `create_auction_open_confirmation_job(db, trade_date, *, worker_instance_id)`：10:00:00 Asia/Shanghai 创建 `auction_open_confirmation:{date}` 任务
- `run_verified_auction_pipeline`：来源留证 → 真值验证 → 共识 capture → scan → aggregate → publish
- `execute_auction_scan_run(job_run_id, trade_date, *, worker_id, lease_epoch)`：调用上述统一入口
- `execute_auction_open_confirmation_run(job_run_id, trade_date, *, worker_id, lease_epoch)`：执行 open_confirmation 任务
- 使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复

### 3.4.1 Scheduler 生产运行拓扑（[P0-3 2026-07-31]）

**远程开发运行入口**：`docker-compose.prod.yml` 的 `worker-after-close` 服务（`WORKER_TYPE=after_close_orchestrator`）。

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

`WORKER_TYPE=auction_scheduler` 分支保留仅用于本地调试，**不是远程开发运行入口**。架构测试 `test_compose_worker_after_close_uses_after_close_orchestrator` 守护 Compose 配置。

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

## 7. 已知缺口与阻断

| 项目 | 状态 | 事实 |
|---|---|---|
| 第二独立真值源 | `blocked_external` | 仓库仅有 mootdx/pytdx，均属通达信供应链 |
| Migration 082 PG upgrade/downgrade/upgrade | `verified` | `c6abcc1` / CI Run `30731828236` 临时 PostgreSQL 通过 |
| 全链 PG Integration | `verified` | 同一 CI Run 完整 PostgreSQL Integration 通过 |
| 正式交易日 E2E | 未执行 | 未部署、不运行本地 Worker、不写生产 |
| 生产发布 | 未执行 | 本轮明确不部署生产 |

## 8. 更新触发条件

当以下任一发生时更新本 Map：
- 锚点合同字段变化（PRD §3）
- 新增/修改 service 或 API
- Migration 修改
- canary 完成后核验状态从"部分核验"升级为"已核验"

## 9. 当前结论

代码层已不再是 quote-capture-only：真值聚合、scan、aggregate、publication pointer、三级页面、
开盘确认和 Review 回流均有真实入口与目标测试。生产仍不能称为闭环，因为独立外部真值源缺失，
统一入口会在 scan 前返回 `blocked_external_auction_truth_source`，且不会写正式 pointer。

## 10. V2.1 竞价锚点编排生命周期（2026-08-05 基线 2267d43，Commit D/I）

> 当前为代码开发阶段，未部署、未跑 PG 集成、未做真实数据验收。

### 10.1 锚点模式决策（Commit D）

- `auction_anchor_service.generate_and_publish_auction_anchors` 为统一入口，
  在一个事务边界内完成「锚点生成 + 校验 + publication 切换」。
- 模式决策：`structure_only`（无 chip）→ `hybrid`（部分 chip ready）→
  `composite`（全部 chip ready）。chip 晚到后升级。
- 生成失败（无可发布 snapshot）软失败返回 `failed`/`publish_failed`，不抛异常。
- 不允许重算 DSA / SMC / momentum；chip 到达后才做 hybrid/composite 升级。

### 10.2 测试分层（Commit I，[Corrective-3 §五] 重新定义）

此前本节称 `test_v21_synthetic_e2e_pure.py` 为 "E2E"，但该测试只组合
`evaluate_closure` / `evaluate_governance` / `decide_auction_mode` 三个决策纯函数，
**不经过 worker、publication adapter 或任何真实编排路径**，不构成 E2E。

- **决策函数集成测试**：`backend/tests/test_v21_readiness_auction_decision_integration.py`
  （原 `test_v21_synthetic_e2e_pure.py`，PURE_UNIT_TEST）。
  覆盖 structure_only→hybrid→composite 模式决策、晚到 chip、failure matrix、
  闭包状态转换。
- **worker 编排服务级测试**：`backend/tests/test_chip_worker_orchestration.py`
  （Corrective-3 新增）。调用真实
  `chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction` /
  `resolve_or_create_chip_run` / `finalize_chip_run`，注入 fake session 与
  fake publish/auction adapter。覆盖 publish→auction 顺序、真实 chip_run_id、
  发布失败不触发 auction composite upgrade、治理 metadata、retry 复用同一领域 run、
  lease 丢失阻断写入。
- PG 依赖部分：`backend/tests/test_v21_synthetic_e2e_pg.py` 标记
  `status = authored_not_executed`、`reason = pg_gate_deferred_during_development`。

### 10.3 状态

- 代码：已实现并 push origin/dev。
- `remote_static_verified = true`、`remote_unit_verified = true`、
  `remote_frontend_build_verified = true`（[Corrective-3 §七] 于隔离 worktree
  精确检出 `f1612f6` 后执行：Ruff 全通过、Mypy 改动文件零错误、
  PURE_UNIT_TEST 52 passed、TSC/ESLint 零错误、vite build 成功）。
- PG 集成 / Migration apply / 部署 / 真实数据验收 / 浏览器验收：未执行（PG gate deferred）。
