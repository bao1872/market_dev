# CHANGE-20260730-018：竞价分析完整链路 — 锚点+扫描+聚合+追踪+前端

状态：进行中（P0调用链闭环+前端集成+合同测试已完成；PG集成待CI终态；canary和部署待后续）
日期：2026-07-30（2026-07-31 P0收口更新）
类型：behavior + contract + architecture + data
领域：竞价分析 / 盘后编排 / 第二金字塔 / 复盘模块 / 前端

## 1. 背景

上一轮将竞价分析冻结为PRD/Map草案。本轮按用户指令实现完整链路：行情质量→第一金字塔core→chip_consensus→auction_anchor→第二金字塔→review→次日auction_final→行业/概念竞价聚合→open_confirmation→tracking review。

## 2. 数据模型（7张表）

### 2.1 Migration 077_auction_analysis
- `auction_anchor_snapshots`: 每日锚点快照（run级状态）
- `auction_anchor_items`: 个股锚点（structure/chip/composite）
- `auction_anchor_publications`: 锚点发布指针
- `auction_scan_runs`: 竞价扫描run（final/opening）
- `auction_instrument_results`: 个股竞价结果
- `auction_scope_results`: 板块/市场竞价聚合
- `auction_event_trackings`: 竞价事件生命周期追踪

### 2.2 Migration 078_review_filter_family_d
- 放宽 market_review 表 filter_family 约束，新增 'D' 族

所有表含 trade_date、algorithm_version、source_core_run_id、source_chip_run_id、price_adjustment_version、coverage、status、reason_codes。

## 3. 服务层

### 3.1 auction_anchor_service.py
- `generate_and_publish_auction_anchors(db, trade_date)`: **[P0-1 统一入口]** 在一个事务边界内完成生成+校验+publication切换。盘后编排、Admin和恢复入口统一调用
- `generate_auction_anchors(db, trade_date)`: 从已发布stock_core读取结构数据生成结构锚点；从chip_consensus读取筹码数据生成筹码锚点；近距离结构+筹码合并为composite；活跃锚点按距离/强度/新鲜度筛选，单股上限20
- `publish_auction_anchors(db, snapshot_id)`: 幂等发布，版本不一致时禁止发布
- `get_published_anchors(db, trade_date)`: 查询已发布锚点
- chip未完成时只生成结构锚点（structure_only）；chip完成后回调重建完整锚点并原子切publication

### 3.1.1 锚点模型修正（P0-6/P0-7）
- 新增 `anchor_key`、`anchor_subtype`、`source_event_id`、`source_time` 字段
- 唯一键改为 `snapshot_id+instrument_id+anchor_key`（旧 `trade_date+instrument+anchor_type+direction` 会吞掉多个OB/BOS）
- `source` 拆为 `source_kind`（core/chip）和 `source_run_id`
- 保存全部有效锚点，扫描仅选择 `is_active/priority_rank`

### 3.2 auction_scan_service.py
- `run_auction_scan(db, trade_date, auction_type)`: 基于冻结锚点分析最终竞价价格的位置迁移和事件
- `update_event_lifecycle(db, scan_run_id)`: 开盘后验证更新事件生命周期（formed→confirmed/weakened/failed/expired）
- 位置分类：below_low/below_trigger/demand_ob/normal/supply_ob/above_trigger/above_high
- 事件类型：dual_breakout/structure_breakout/chip_repricing/support_confirm/resistance_blocked/test_upper/test_lower/inside_open/insufficient_participation/structure_chip_conflict/anchor_insufficient/anchor_expired
- 参与度分级：abnormal_low/low/normal/high/abnormal_high

### 3.3 auction_aggregation_service.py
- `compute_auction_aggregation(db, scan_run_id)`: 计算市场/行业/概念三级聚合
- 状态标签：full_repricing/leader_driven/initial_diffusion/resistance_high_open/support_repair/full_breakdown/high_divergence/inconclusive
- 置信度：high(valid>=20且coverage>=0.8)/medium/low
- 所有比例同时返回分子和分母

### 3.4 board_analysis_service.py 扩展（第二金字塔V2）
- 新增 payload["pyramid_v2"] 子键
- 状态迁移矩阵、新鲜度密度、扩散度、集中度（Top3/Top5/HHI）、内部离散度、相对强弱
- 概念额外：核心/边缘成员、置信度

### 3.5 after_close_orchestrator.py 接入
- 在stock_core发布后、market_aggregation之前插入auction_anchor生成
- 接入顺序：stock_core→chip_consensus→auction_anchor→market_aggregation→review
- chip软失败：succeeded/partial生成完整锚点；failed/timeout生成structure_only；chip恢复后回调重建
- 失败不影响core，标记为optional_failure

### 3.5.1 auction_scheduler_service.py（P0-3 新增）
- 09:25:05 Asia/Shanghai 创建 `auction_final:{date}` 任务
- 10:00:00 Asia/Shanghai 创建 `auction_open_confirmation:{date}` 任务
- 使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复
- 接入现有 after_close_orchestrator Worker（不新建容器），WORKER_TYPE=auction_scheduler

### 3.6 复盘逻辑修复
- metric_engine.py: fp_segment_change_pct全空时P指标value=None，readiness=unavailable
- 新增D族筛选器（D1-D5）：迁移/新鲜度/扩散/集中度/相对强弱
- review_scan_service注入pyramid_v2数据

## 4. API

6个端点：
- GET /auction — 市场级页面数据
- GET /auction/board/{board_id} — 板块级页面数据
- GET /auction/stock/{symbol} — 个股级页面数据
- GET /auction/anchors/{trade_date} — 锚点查询
- POST /admin/auction/scan — 触发竞价扫描（admin only）
- POST /admin/auction/anchors — 触发锚点生成（admin only）

## 5. 前端

三级页面：
- /auction — 市场级（行业概念排行、突破破位广度、参与度、集中度）
- /auction/board/:boardId — 板块级（分布、锚点迁移、贡献/反例/未跟随、样本和置信度）
- /auction/stock/:symbol — 个股级（昨日金字塔、结构/筹码锚点、竞价位置、参与度、趋势背景、开盘状态）

### 5.1 P0-FE 修复（2026-07-31）
- DTO 返回 `symbol` 和 `name`，导航使用 symbol（禁止 UUID）
- 用户一级导航新增 `/auction` 入口
- `/review` 新增 "竞价回流" 阶段（第6阶段），展示 AuctionBackflowPanel
- AuctionBackflowPanel 展示四维度：分布（event_type/lifecycle）、迁移、新鲜度、集中度+事件回流
- 修复 TS6133 未使用变量错误（changeCls/SCOPE_TYPE_LABELS）

## 6. 测试

- 172个竞价纯单元测试通过（anchor 70 + scan 102）
- 10个竞价前端合同测试通过（auctionContract.test.ts）
- 27个导航/路由合同测试通过
- PG集成测试15个（CI环境运行，0 skipped）
- 覆盖：多OB不丢失、generate+publish原子性、chip完成后刷新、scan幂等/恢复、lifecycle多阶段（formed/confirmed/continued/weakened/failed/transformed/expired）、UUID/symbol前端合同、Scheduler 09:25/10:00触发窗口

## 7. 文档更新

- docs/prd/75-auction-analysis.md: DRAFT → 确认，扩展为完整实现要求
- docs/maps/75-auction-analysis.md: 设计草案 → 已实现
- docs/maps/30-after-close.md: 添加auction_anchor接入
- docs/maps/70-review.md: 添加D族筛选器和pyramid_v2
- docs/changes/INDEX.md: 新增018条目

## 8. 未完成

- PG集成测试CI终态（本地跳过，CI临时PostgreSQL运行）
- 09:25 BarMinute 数据源覆盖率验证（需连接生产DB只读检查）
- canary（少量股票/1个行业/1个概念，需dev/staging环境）
- 全量回填/计算
- 部署（无隔离dev/staging时标记BLOCKED）
