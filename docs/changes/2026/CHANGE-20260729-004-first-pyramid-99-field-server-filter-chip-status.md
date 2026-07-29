# CHANGE-20260729-004：第一金字塔 99 字段服务端筛选排序 + 筹码共识结构化 chipStatus

状态：进行中（代码+目标测试+TSC+ESLint 通过，浏览器真实链路验收待用户手工）
日期：2026-07-29
类型：behavior+architecture
领域：行情体验/量化模型

负责人：TRAE CN (Local Native)

相关 PRD：

- `../../prd/40-market-stock-experience.md`：MX-20（列表视图 99 字段服务端筛选排序）
- `../../prd/20-quant-model.md`：QM-01～QM-43（第一金字塔结构化状态）

相关 Maps：

- `../../maps/40-market-stock-experience.md`
- `../../maps/20-quant-model.md`

相关 Rules：

- `../../../rules/40-testing-quality.md`
- `../../../rules/20-market-data-indicators.md`

相关提交或 PR：

- 待 push 后回填（本轮功能修复提交）

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮实现两项 P0 行为变化：(1) 列表视图第一金字塔 99 字段从“统一 sortable=false/filterable=false”改为全部支持服务端筛选与排序，建立唯一 `FP_QUERY_FIELD_SPECS` 白名单并在分页前应用；(2) 第一金字塔 DTO 增加结构化 `chipStatus`（state/reasonCode/reasonText/computedAt），页面显示真实原因，不再统一显示“暂无有效筹码峰”。深科技（000021）筹码共识不可用根因确认为 `M15_BARS_INSUFFICIENT`（15m bars 实际 338 根 < 阈值 4000），DTO 现返回 `reasonText="15 分钟数据不足（338 根，需 ≥4000）"`。

## 2. 背景与问题

变化前：

- 列表视图 99 列被统一标记 `sortable=false` / `filterable=false`，前端无法对第一金字塔字段排序或筛选；
- 即便前端排序，也只对当前页数据生效，违反“服务端分页前完成筛选排序”的契约；
- 第一金字塔 DTO 无结构化筹码状态字段，前端只能根据 `chipConsensus=null` 统一显示“暂无有效筹码峰”，掩盖真实原因（15m 不足 / chip job 未运行 / 无有效峰等）；
- 深科技筹码共识长期“暂不可用”，但根因未明确。

风险：

- 用户无法在列表页按第一金字塔字段筛选排序，体验受损；
- 筹码不可用原因不透明，用户无法判断是数据问题、计算问题还是预期行为；
- 前端传任意 SQL 字段或 JSON 路径存在注入风险。

触发本次变化的事实或证据：

- `stock_chip_consensus_snapshots` 查询：深科技（instrument_id=2196e868-b0a1-4113-a3ad-96af7bb1092a，trade_date=2026-07-28）的 15m bars 数=338，Node Cluster 返回 `INPUT_CONTRACT_VIOLATION`，根因 `M15_BARS_INSUFFICIENT`；
- 前端列定义代码确认 `sortable=false / filterable=false`；
- `FirstPyramidSnapshot` DTO 无 `chipStatus` 字段。

## 3. 变化前

- `firstPyramidColumns.tsx` 所有列 `sortable: false, filterable: false`；
- `/market/stocks` 接口无 `fp_filter` / `fp_sort` 参数；
- `FirstPyramidSnapshot` DTO 无 `chipStatus`；
- `FirstPyramidPanel.tsx` ChipVisualCard 不可用时固定显示“可选维度 · 暂无有效筹码峰”；
- `_map_reason_code` 将 `INPUT_CONTRACT_VIOLATION` 归入 `NODE_COMPUTE_FAILED`，掩盖 15m 不足真实原因。

## 4. 变化内容

### 4.1 第一金字塔 99 字段服务端筛选排序

- 后端新增唯一规格表 `FP_QUERY_FIELD_SPECS`（`backend/app/services/first_pyramid_flatten.py`）：每个 `fp_*` 字段定义数据类型、JSON 路径、允许操作符；断言覆盖全部 99 键；
- `market_stocks_service.get_market_stocks` 新增 `fp_filter` / `fp_sort` 参数，解析为 `FpFilterSpec` / `FpSortSpec`，通过 JSON 路径标量子查询在分页前应用筛选与排序；
- 操作符规则：数字/百分比 `gt/gte/lt/lte/eq/between`；文本 `contains/not_contains/eq`；日期 `比较/区间`；布尔/枚举 `eq`；全部支持 `empty/not_empty`；
- 排序 `asc/desc` 均 `NULLS LAST`，固定 `symbol` 作为第二排序键保证翻页稳定；
- `/market/stocks` 接口暴露 `fp_filter` / `fp_sort` 查询参数（`backend/app/api/market.py`）；
- 前端 `firstPyramidColumns.tsx` 启用 `sortable: true, filterable: true`，提供 `sortValue` / `filterValue` 提取原始值；
- 前端 `firstPyramidQuerySerializer.ts` 序列化筛选/排序条件为 URL 参数；
- `MarketWorkspacePage.tsx` 在 `marketStocksParams` 中传递 `fp_filter` / `fp_sort`；
- `MarketStocksQueryParams` 接口新增 `fp_filter` / `fp_sort` 字段。

### 4.2 第一金字塔筹码共识结构化 chipStatus

- `backend/app/schemas/first_pyramid.py` 新增 `ChipStatus` DTO（`state`/`reasonCode`/`reasonText`/`computedAt`），定义 `CHIP_STATUS_STATES` 与 `CHIP_STATUS_REASON_CODES`；
- `FirstPyramidSnapshot.chipStatus` 字段新增；
- `backend/app/services/first_pyramid_service.py` 新增 `_build_chip_status(chip)`，根据 `ChipConsensusResult` 状态映射到：
  - `chip is None` → `pending` / `CHIP_JOB_PENDING`
  - `error 含 INPUT_CONTRACT_VIOLATION` → `unavailable` / `M15_BARS_INSUFFICIENT`
  - `error 含 daily_bars` → `unavailable` / `DAILY_BARS_INSUFFICIENT`
  - `error 含 profile_empty` → `unavailable` / `NO_VALID_PEAK`
  - 其他 error → `failed` / `CHIP_JOB_FAILED`
  - `chip.chip.available=True` → `ready`
- `backend/app/api/stock_context.py` `_map_reason_code` 区分 `INPUT_CONTRACT_VIOLATION` / `INSUFFICIENT_15M_HISTORY` → `NODE_15M_INSUFFICIENT`（之前误归 `NODE_COMPUTE_FAILED`）；
- 前端 `endpoints.ts` 新增 `ChipStatus` 接口与 `FirstPyramidSnapshot.chipStatus` 字段；
- 前端 `firstPyramidViewModel.ts` `FirstPyramidVM` 新增 `chipStatus` 字段并透传；
- 前端 `FirstPyramidPanel.tsx` `ChipVisualCard` 不可用时显示 `chipStatus.reasonText`，缺省才退回中性文案。

## 5. 变化后

- 列表视图 99 字段全部支持服务端筛选与排序，排序与筛选在分页前完成，翻页稳定；
- 后端通过 `FP_QUERY_FIELD_SPECS` 白名单严格限制可查询字段与操作符，非法字段/操作符返回 422；
- 第一金字塔 DTO 携带结构化 `chipStatus`，前端显示真实原因（如“15 分钟数据不足（338 根，需 ≥4000）”），不再统一显示“暂无有效筹码峰”；
- 深科技筹码共识不可用原因明确：15m bars 数量不足，需后续补数据（不在本轮处理）。

当前完整实现细节以相关 Maps 为准。

## 6. 影响范围

### 用户行为

- 列表视图可按任意第一金字塔字段筛选与排序；
- 个股详情页筹码共识不可用时显示真实原因而非统一文案。

### API 或契约

- `GET /market/stocks` 新增 `fp_filter` / `fp_sort` 查询参数；
- `FirstPyramidSnapshot` 新增 `chipStatus: ChipStatus | null` 字段（可选，向后兼容）。

### 数据

- 无 schema 变化；筛选与排序基于现有 `StockFeatureSnapshot.summary_payload` JSON 路径。

### 前端

- `firstPyramidColumns.tsx` 启用 sortable/filterable；
- `firstPyramidQuerySerializer.ts` 新增；
- `MarketWorkspacePage.tsx` 传递 fp 参数；
- `FirstPyramidPanel.tsx` 显示 chipStatus.reasonText；
- `firstPyramidViewModel.ts` 透传 chipStatus。

### 后端

- `first_pyramid_flatten.py` 新增 `FP_QUERY_FIELD_SPECS`；
- `market_stocks_service.py` 实现 fp_filter/fp_sort 解析与应用；
- `market.py` 暴露查询参数；
- `first_pyramid.py` schema 新增 ChipStatus；
- `first_pyramid_service.py` 新增 `_build_chip_status`；
- `stock_context.py` 修正 `_map_reason_code`。

### Worker 与任务

- 无变化；chip job 仍为盘后非阻塞独立任务。

### 部署与运行

- 无 migration；无新 worker；无配置变化。

## 7. 迁移与兼容

- 无 Migration；
- 无历史回填；
- `chipStatus` 为可选字段，旧客户端忽略不影响；
- `fp_filter` / `fp_sort` 为可选查询参数，不传则行为不变。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 后端单元测试 | test_chip_status / test_first_pyramid_flatten / test_first_pyramid_contract | PASS（109 passed） | 本地 PURE_UNIT_TEST=1 pytest |
| 后端 Ruff | 修改的 8 个文件 | PASS（All checks passed） | 本地 ruff check --no-cache |
| 前端 TSC | 全量 | PASS（无输出） | 本地 npx tsc --noEmit |
| 前端 ESLint | 修改的 6 个文件 | PASS（无输出） | 本地 npx eslint |
| 前端 contract | test:contract | 未验证（本地 Node 20.10 不支持 --experimental-strip-types，待 CI Node 21+ 运行） | npm run test:contract 输出 |
| 深科技根因 | stock_chip_consensus_snapshots 只读查询 | M15_BARS_INSUFFICIENT（338 < 4000） | 数据库只读查询（本轮不写库） |
| 浏览器真实链路 | 深科技 + 一只正常筹码股 | 待用户手工验收 | 用户手工 |

不得用“代码看起来正确”代替运行证据。

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | 无变化（MX-20 已确认 99 字段筛选排序需求） |
| Maps | 待更新 `maps/40-market-stock-experience.md`（FP 服务端筛选排序入口）与 `maps/20-quant-model.md`（chipStatus DTO） |
| Runbooks | 无变化 |
| Rules | 无变化 |

## 10. 回滚方案

- 代码可回滚：恢复 `firstPyramidColumns.tsx` sortable=false/filterable=false；移除 `fp_filter`/`fp_sort` 参数；移除 `chipStatus` 字段；
- 数据无需回滚：无 schema 变化；
- 回滚后验证：列表视图恢复无筛选排序；筹码卡恢复统一文案。

## 11. 遗留问题与风险

- 深科技 15m bars 不足（338 < 4000）需后续补数据，不在本轮处理；
- 前端 contract 测试待 CI（Node 21+）运行；
- 浏览器真实链路验收待用户手工执行；
- `docs/current` 标记为 legacy 只读，后续另行迁移（本轮不处理）。

## 12. 后续变化

- 无（待深科技补数据后另行记录）。
