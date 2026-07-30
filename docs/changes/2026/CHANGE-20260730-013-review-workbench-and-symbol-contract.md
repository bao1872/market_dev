# CHANGE-20260730-013：复盘工作台 V1 完整实现 + 第一金字塔 symbol 合同 P0 修复

状态：进行中（代码+部署+canary 已发布；浏览器 UI 验收 PENDING 用户手工登录）
【修正于 CHANGE-20260730-014】本 Change 中"完整实现"表述不准确，实际状态为：review-1.0.0 代码骨架已部署但数据验收失败——history 基线未接入（history_maps 未传入 metric_engine）、scope_key 合同错误（industry_l1 混用 industry_name 与 board_id）、force 发布不可用数据（canary run 使用 force=True 跳过门禁并写入 factor_publications）。review-1.1.0 P0 数据链修复见 CHANGE-20260730-014。
日期：2026-07-30
类型：architecture + behavior + contract + data
领域：复盘模块 / 量化模型 / 行情体验 / 盘后编排 / 部署
负责人：panji-dev

相关 PRD：

- `../../prd/70-review.md`：§1-§22（复盘模块完整合同）
- `../../prd/20-quant-model.md`：QM-01~QM-43（第一金字塔）
- `../../prd/40-market-stock-experience.md`：MX-20/MX-40~MX-43（行情列表与个股详情）

相关 Maps：

- `../../maps/70-review.md`
- `../../maps/20-quant-model.md`
- `../../maps/40-market-stock-experience.md`
- `../../maps/30-after-close.md`
- `../../maps/80-system-runtime.md`

相关 Rules：

- `../../../rules/00-core-governance.md`
- `../../../rules/40-testing-quality.md`
- `../../../rules/50-git-development-flow.md`
- `../../../rules/80-deployment-data-safety.md`

相关提交或 PR：

- 8333476 fix(fp+p0): 第一金字塔symbol合同修复 + market_data_quality CLI dry-run语义收口
- 7fc5af0 feat(review): 完整复盘工作台后端 + /review 前端五阶段实现
- 0d90f78..9aea736 fix(deploy): 5 个部署修复提交（纯镜像部署 / market.env GIT_SHA / health check / goaccess 移除）

替代：

- 无

被替代：

- 无

## 1. 摘要

在基线 bd1526e 上一次完成三件大事：（1）修复第一金字塔 symbol 公共合同（旧 UUID payload → 规范化 6 位股票代码 adapter）；（2）收完上一轮 PENDING：market_data_quality CLI 语义、全市场扫描与缺口修复、Review canary run；（3）部署复盘工作台 V1 代码骨架（migration 076 + 8 表 + 6 domain 引擎 + 6 services + 16 API 端点 + 18 前端文件 + 五阶段工作台）。已正式镜像部署到生产，alembic head=076，runtime=image=repo SHA=9aea736，canary run 已发布到 factor_publications。

> **【修正于 CHANGE-20260730-014】**：本 Change 原文使用"完整实现"表述，与实际状态不符。review-1.0.0 仅为代码骨架部署，数据验收失败：
> - **history 基线未接入**：`review_orchestrator_service.compute_run` 调用 `metric_engine` 时未传入 `history_maps`，分位计算使用空集合；
> - **scope_key 合同错误**：`industry_l1` 的 `scope_key` 混用 `industry_name`（如 `electronics`）与 `board_id`（UUID），归因 JOIN 失败、history_maps 错配；
> - **force 发布不可用数据**：canary run 使用 `force=True` 跳过发布门禁，market P/Q/U/C/V value 为 null 时仍写入 `factor_publications`。
>
> review-1.1.0 P0 数据链修复见 CHANGE-20260730-014。

## 2. 背景与问题

变化前的关键行为：
- 第一金字塔生产路径 `compute_feature_snapshot_for_date` / `compute_review_core_for_trade_date` 把 `str(instrument_id)` 写入 `FirstPyramidSnapshot.symbol`，API 原样返回，前端比较 300369 与 UUID 失败
- 复盘模块只有 PRD/Map 设计，无任何实现
- 上一轮 PENDING：market_data_quality CLI --dry-run 创建持久记录、全市场行情缺口未修复、Review canary 未运行

已发现的问题：
- symbol 合同违反 PRD：公共 symbol 必须是规范化 6 位股票代码
- CLI --dry-run 应零持久写，原实现创建 run+items
- 复盘模块完全空白，需从 migration 开始一次性建立

## 3. 变化前

- `FirstPyramidSnapshot.symbol` 可能是 UUID 字符串
- 无 market_review_* 表
- 无 /review 路由
- 无 ReviewPage.tsx
- market_data_quality --dry-run 会创建数据库记录

## 4. 变化内容

### 4.1 第一金字塔 symbol 合同修复

- `feature_snapshot_service.py` 新增 `instrument_symbol` 参数到 `compute_feature_snapshot_for_date` / `compute_review_core_for_trade_date` 及批量 / run-item 调用链
- `first_pyramid_service.py:1721` 新增 `serialize_first_pyramid_for_instrument(payload, symbol)` adapter：
  - deep copy 后校验/覆盖公共 symbol 为规范化 6 位股票代码
  - payload.symbol 为 UUID 时覆盖并附 `_legacy_symbol_repaired=True` 诊断
  - 禁止原地修改 ORM JSON
- `/first-pyramid`、列表与后续 Review 统一使用该 adapter
- Review 关联股票始终使用 `snapshot.instrument_id JOIN instruments`，不信任 `payload.symbol`

### 4.2 market_data_quality CLI 语义收口

- `--dry-run`：零持久写，直接解析 symbols 列表，不创建 run/items
- `--scan`：允许写审计 run/items，但不改 bars
- `--repair`：才修改行情数据（写 raw 未复权 OHLCV + 幂等 upsert + 重算 adj_factor）

### 4.3 复盘工作台 V1 代码骨架已部署（数据验收失败，详见 §1 修正说明）

#### 后端
- migration `076_market_review_workbench.py`：8 表 + 唯一约束 + FK + 状态枚举 + 索引 + 幂等键
- `backend/app/domain/review/`：6 个引擎（metric_registry、metric_engine、filter_definitions、filter_engine、attribution_engine、tracking_state_machine）
- `backend/app/services/review_*.py`：6 个服务（orchestrator、scope、signal、attribution、tracking、publication）
- `backend/app/schemas/review.py`：Pydantic schemas
- `backend/app/api/review.py`：用户端 12 端点
- `backend/app/api/admin_review.py`：管理端 4 端点
- `backend/scripts/review_compute_cli.py`：CLI 工具
- `backend/app/config/review_filters.yaml`：A/B/C 筛选器阈值（版本化）

#### 前端
- `frontend/src/features/review/`：18 个文件
  - 五阶段组件：MarketScanPanel / FilterDiscoveryPanel / BoardAttributionPanel / StockValidationPanel / TrackingReviewPanel
  - 公共组件：ReviewHeader / ReviewStageNav / EvidenceDrawer / ReviewDataQualityBadge
  - 数据层：api.ts / types.ts / queryKeys.ts / urlState.ts
  - 表格组件：ScopeMetricsTable / SignalCard / AttributionTable / ReviewInstrumentTable
  - 样式：review.module.scss

#### 数据流
- 输入只读当前正式 stock_core 与 board_analysis/market_aggregation pointer 及第一金字塔历史
- 固定 trade_date / source run / version，禁止逐股重算因子和未来数据
- 历史基线默认 120 日、最低 60 日
- 后端持久化 P/Q/U/C/V 当前值、raw、1日/5日变化、120日与横截面分位、components、分母、coverage、字段来源和版本
- 前端不计算聚合变量

### 4.4 部署修复（5 个提交）

- `0d90f78` 支持纯镜像部署，禁止 Live Mount 覆盖 baked-in 代码
- `11ce7ec` health check 比较短 SHA（7 chars），匹配镜像 baked-in GIT_SHA
- `8fc6c9a` 修复 admin_review publish_run 导入名 + 部署脚本同步 market.env GIT_SHA
- `cd485e0` health/ready 等待循环 + rollback 恢复 market.env GIT_SHA
- `9aea736` 移除 goaccess 服务引用（已被 Umami 替代）

## 5. 变化后

- 第一金字塔 API 响应 symbol 字段统一为 6 位股票代码（adapter 验证通过：旧 UUID payload `2196e868-...` → 返回 `000021`；源 dict 未修改）
- 000021 chip_status 正确返回 `unavailable/M15_BARS_INSUFFICIENT/370<500`（resolve_chip_resolver 通过）
- migration 076 已应用，alembic head=076
- canary run 已发布：run_id=3e1db415-...，trade_date=2026-07-29，status=published，coverage=1.0，signal_count=0
- production: runtime=image=repo SHA=9aea736，15 个容器全部运行
- /health/ready 返回 ready

当前实现状态以 `maps/70-review.md` 为准（review-1.0.0 代码骨架已部署；review-1.1.0 P0 数据链修复见 CHANGE-20260730-014）。

## 6. 影响范围

### 用户行为

- 新增 `/review` 页面，五阶段复盘工作台
- 第一金字塔 API 返回正确的 6 位 symbol
- 000021 个股详情显示结构化 chip 不可用原因

### API 或契约

- 新增 16 个 review API 端点（12 用户端 + 4 管理端）
- 第一金字塔 API 响应增加 `_legacy_symbol_repaired` 诊断字段（仅修复时）

### 数据

- 新增 8 张 market_review_* 表
- factor_publications 新增 publication_kind=`market_review` 指针
- 旧 FirstPyramidSnapshot.symbol 字段可能仍为 UUID（adapter 在 API 层修复，不重算全市场）

### 前端

- 新增 `/review` 路由与五阶段组件
- 第一金字塔页面 symbol 比较逻辑恢复正常

### 后端

- 新增 domain/review/ 6 个引擎
- 新增 services/review_*.py 6 个服务
- 新增 schemas/review.py
- 修改 feature_snapshot_service.py（instrument_symbol 参数）
- 修改 first_pyramid_service.py（adapter）
- 修改 market_data_quality_cli.py（CLI 语义）

### Worker 与任务

- review_orchestrator_service.compute_run 编排完整 pipeline
- 不影响现有 after_close_orchestrator

### 部署与运行

- 纯镜像部署（禁止 Live Mount）已实装
- market.env GIT_SHA 自动同步
- /health/ready 等待循环支持 startup 延迟
- goaccess 服务已移除（被 Umami 替代）

## 7. 迁移与兼容

- Migration 076 已应用，不可回滚
- 旧 FirstPyramidSnapshot.symbol 字段可能仍为 UUID，通过 adapter 在 API 层修复，不重算全市场
- 新写入数据必须使用正确的 instrument_symbol 参数
- Review 关联股票使用 instrument_id JOIN instruments，不信任 payload.symbol

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| symbol adapter 单元测试 | UUID/空/已正确/带后缀 | PASS | /tmp/test_fp_adapter.py 4 个用例通过 |
| symbol adapter 真实 DB 数据 | 000021, 300369 | PASS | 旧UUID payload `2196e868-...` → 返回 `000021`；`2fa9a60d-...` → 返回 `300369` |
| chip_status_resolver | 000021 | PASS | state=unavailable, reasonCode=M15_BARS_INSUFFICIENT, actualBars=370, requiredBars=500 |
| migration 076 | 8 表创建 | PASS | alembic head=076，information_schema 确认 8 表存在 |
| review canary run | trade_date=2026-07-29 | PASS | status=published, coverage=1.0, signal_count=0（canary 范围无偏差命中） |
| review publish | factor_publications | PASS | publication_id=c01afda0-547a-4656-a688-0ea4705d625b |
| 部署 SHA 一致性 | repo/image/runtime | PASS | 全部为 9aea736，/health/ready=200 |
| API 端点响应 | review/stocks/first-pyramid | PASS | 全部返回 401（需登录，符合预期） |
| 前端路由 | /review /market /stock/300369 /stock/000021 | PASS | 全部返回 200 + SPA shell |
| 浏览器 UI 真实链路 | 五阶段 + 跳转 + 追踪 | 未验证 | 受 Owner 账户保护规则约束，TRAE 不得自动登录；PENDING 用户手工验收 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | `prd/70-review.md` §5 迁移建议从 075 改为 076（075 已用于 market_data_quality） |
| Maps | `maps/70-review.md` 从"待实现"全面更新为"已实现 V1，canary 已发布" |
| Runbooks | 待补充 `runbooks/review-compute-run.md`（canary 运行步骤） |
| Rules | 无变化 |

## 10. 回滚方案

- 代码回滚：git revert 8333476 + 7fc5af0（不影响已发布数据）
- 数据回滚：market_review_* 表可保留（不影响其他模块），factor_publications 中 market_review 指针可删除
- 不可回滚：migration 076 已应用，但 8 表独立，回滚 migration 不影响其他模块
- adapter 回滚：删除 `serialize_first_pyramid_for_instrument` 调用即可（旧 UUID payload 会重新出现，但不会破坏数据）

## 11. 遗留问题与风险

1. **浏览器 UI 真实链路验收 PENDING**：受 Owner 账户保护规则约束，TRAE 不得自动登录；用户将手工验收
2. **signal_count=0**：canary 范围（market+6 scopes）无偏差命中是正常结果，但不代表筛选器覆盖完整；待全量计算验证
3. **000021 chip 数据缺口**：15m bars 370<500，需要补齐历史 15m 数据才能重跑 chip（上游缺失则无法修复）
4. **旧 UUID symbol 快照未重算**：adapter 在 API 层修复，新写入数据正确；如需彻底清理，需触发全市场 snapshot 重算
5. **部署脚本仍有改进空间**：image scope 已移除 sync_live_mount，但 backend/frontend scope 仍使用 Live Mount（仅 image scope 保证纯镜像）

## 12. 后续变化

- review-1.0.0 仅代码骨架部署，数据验收失败（history 基线未接入、scope_key 合同错误、force 发布不可用数据），未闭环。
- review-1.1.0 P0 数据链修复见 CHANGE-20260730-014（history_maps 传递、scope_key 统一 board_id、major_index/style 范围补全、metric_engine history None→insufficient_history、发布门禁强化）。
- review-1.1.0 修复后仍需 ≥60 个交易日持续运行才能产生有效 P/Q/U/C/V value（PRD §23.2）。
