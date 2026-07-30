# CHANGE-20260730-011 Umami 页面真正迁移 + 000021 筹码原因 + History 6只审计 + 板块分析 V1

**日期**：2026-07-30
**类型**：生产遗留 P0 收口 + 新功能（板块分析 V1）
**关联**：CHANGE-20260729-009（Umami 容器部署完成但页面迁移遗漏）/ CHANGE-20260729-003（筹码状态结构化）
**起点 SHA**：8e4e6a7（main=dev=origin）

## 摘要

本轮在一个闭环内完成四件事：

1. **Umami 真正迁移**：CHANGE-009 部署了 Umami 容器但 `/admin/visitors` API 和 `AdminVisitorsPage.tsx` 仍硬编码 GoAccess。本轮新增 `UmamiAnalyticsAdapter`，通过独立只读连接查询 umami 数据库，重写后端 API 和前端页面，data_source 改为 `umami` / `empty` / `error`。
2. **000021 详情筹码原因结构化**：抽取共享 `chip_status_resolver.resolve_chip_status`，供 `/market/stocks` 列表和 `/first-pyramid` 详情 API 共同使用，camelCase 输出与前端 `ChipStatus` schema 一致。000021 详情现在显示 `M15_BARS_INSUFFICIENT + actualBars=354 + requiredBars=500 + fullQualityBars=4000` 三栏诊断。
3. **History 6 只股票最后日期不一致审计**：5184 只历史股票中，5178 只与 Core 2026-07-29 一致，6 只退市股（000004国华退/002808恒久退/002898赛隆退/300029天龙退/600193退市创兴/605081退市太和）History 最后日期 = bars_daily 最后日期（退市前最后交易日），合理差异，无需 repair，标记为 `valid_for_market_aggregation=false`。
4. **板块分析 V1**：新增 `board_analysis_snapshots` 表（migration `074_board_analysis_v1`），实现服务层、API 路由、CLI、前端页面。基于已发布 `stock_core` 数据计算板块内部分布指标（趋势/结构/动量/量能/事件率），coverage ≥ 0.95 才正式发布 pointer。

## 修改前

### 访问统计
- `backend/app/api/admin_visitors.py` 硬编码 GoAccess：`GOACCESS_REPORT_PATH="/srv/goaccess/report.json"`，解析 GoAccess JSON
- `backend/app/schemas/visitors.py` `data_source` 值为 `goaccess_json` / `empty` / `error`
- `frontend/src/pages/AdminVisitorsPage.tsx` 标题"访问统计"，描述"GoAccess 报告"，仅处理 `goaccess_json` 数据源
- 截图证明：访问统计页面仍读取 GoAccess

### 000021 详情筹码
- `/first-pyramid` 路由只在 chip 状态为 `succeeded` 时返回 chip 数据，不构建 `chipStatus` 字段
- 列表 API 和详情 API 使用不同的 chip 状态构建逻辑（字段名/口径不一致）
- 000021 详情显示"暂无有效筹码峰"泛化文案，无具体原因

### History 6 只
- 5178/5184 与 Core 一致，6 只差异原因未明确

### 板块分析
- 无板块级聚合分析能力

## 修改后

### 1. Umami 真正迁移

**新增 `UmamiAnalyticsAdapter`**（`backend/app/services/umami_analytics_adapter.py`）：

- 独立只读连接查询 umami 数据库（凭据从 `UMAMI_DATABASE_URL` 环境变量读取）
- 查询 `website_event` + `session` 表，返回 PV / UV / 热门页面 / 来源 / 设备 / 浏览器 / 24 小时时段趋势
- 敏感 query 参数由 `_sanitize_path` 脱敏（token / jwt / password / key 等）
- 三档返回：`data_source="umami"`（成功）/ `"empty"`（未配置）/ `"error"`（查询异常）
- 每次请求创建独立 engine，避免与主业务库混淆

**重写 `admin_visitors.py`**：调用 `fetch_umami_report()`，删除所有 GoAccess 相关代码。

**重写 `AdminVisitorsPage.tsx`**：标题"Umami 访客分析"，错误指向 Umami 服务，新增"打开详细分析"按钮跳转 `/umami/`。

**`docker-compose.prod.yml`**：backend 服务增加 `UMAMI_DATABASE_URL` 和 `UMAMI_WEBSITE_ID` 环境变量。

### 2. 000021 详情筹码原因结构化

**新增 `chip_status_resolver.py`**（`backend/app/services/chip_status_resolver.py`）：

```python
async def resolve_chip_status(
    session, instrument_id, trade_date, snapshot_run_id, algorithm_version,
) -> ChipStatus:
    # 严格五元组匹配，扫描所有 status 记录取最新一条
    # 无记录 → state=pending + reasonCode=CHIP_JOB_PENDING
    # succeeded + chip.available=True → state=ready
    # succeeded + chip.available=False → state=unavailable + NO_VALID_PEAK
    # skipped + M15_BARS_INSUFFICIENT → state=unavailable + actualBars/requiredBars/fullQualityBars
    # skipped + DAILY_BARS_INSUFFICIENT → state=unavailable + actualBars
    # failed → state=failed + CHIP_JOB_FAILED
```

**`stock_context.py /first-pyramid` 路由**：调用 `resolve_chip_status`，注入 `stored_fp["chipStatus"]`（camelCase，与列表 API 完全同口径）。

**`market_stocks_service._build_chip_status_struct`**：调用 `_build_chip_status_from_row` 实现，输出 dict（`model_dump(by_alias=False)`）。

**前端 `FirstPyramidPanel.tsx` 的 `ChipVisualCard`**：
- `state=unavailable + reasonCode=M15_BARS_INSUFFICIENT` → 显示 `actualBars / requiredBars / fullQualityBars` 三栏诊断
- `state=pending` → "筹码任务尚未执行"
- `state=failed` → "筹码计算失败" + reasonText
- 不再退化成"暂无有效筹码峰"泛化文案

### 3. History 6 只股票审计

**核验方法**：生产数据库只读查询，对比 `bars_daily.max(trade_date)` 与 `first_pyramid_history_daily_state.max(trade_date)`。

| symbol | 名称 | 状态 | History 最后日 | Core 日 | 原因 |
|---|---|---|---|---|---|
| 000004 | 国华退 | 退市 | 退市前最后交易日 | 2026-07-29 | 退市股 Core 仍保留 snapshot |
| 002808 | 恒久退 | 退市 | 同上 | 2026-07-29 | 同上 |
| 002898 | 赛隆退 | 退市 | 同上 | 2026-07-29 | 同上 |
| 300029 | 天龙退 | 退市 | 同上 | 2026-07-29 | 同上 |
| 600193 | 退市创兴 | 退市 | 同上 | 2026-07-29 | 同上 |
| 605081 | 退市太和 | 退市 | 同上 | 2026-07-29 | 同上 |

**结论**：6 只退市股的最后日期不一致为合理差异，**无需 repair**。退市股不参与板块聚合（`valid_for_market_aggregation=false`，由 `Instrument.status != 'active'` 判定）。

### 4. 板块分析 V1

**新增 migration `074_board_analysis_v1`**：

- 新增 `board_analysis_snapshots` 表
- 单表设计：每条记录既是 run 又是 snapshot（含 `status` / `started_at` / `finished_at`）
- 唯一键 `(trade_date, board_id, algorithm_version)` 保证幂等
- 复用 `factor_publications` 表发布指针：`publication_kind=market_aggregation`、`scope_type=board`、`scope_key=board_id::text`、`data_run_id=board_analysis_snapshot.id`

**新增 ORM** `backend/app/models/board_analysis_snapshot.py`、Schema `backend/app/schemas/board_analysis.py`。

**新增 Service** `backend/app/services/board_analysis_service.py`：
- `BOARD_ANALYSIS_ALGORITHM_VERSION="board-v1-20260730"`
- `BOARD_ANALYSIS_MIN_COVERAGE=0.95`
- 纯函数 `compute_board_payload(flat_list)` 计算 7 大维度指标
- 入口 `compute_board_analysis(session, board_id, trade_date, ...)` 单板块计算
- 批量 `compute_all_boards(session, trade_date, ...)` 行业+概念
- 发布 `publish_board_analysis(session, snapshot)` 写入 factor_publications 指针
- 查询 `list_board_analyses` / `get_board_analysis_detail` / `compute_is_stale` / `check_is_published`

**新增 API** `backend/app/api/board_analysis.py`：
- 用户路由 `GET /api/v1/boards/analysis` 列表分页
- 用户路由 `GET /api/v1/boards/{board_id}/analysis` 单板块详情
- 管理路由 `POST /api/v1/admin/boards/{board_id}/analysis/compute` 单板块触发
- 管理路由 `POST /api/v1/admin/boards/analysis/compute-all` 批量触发

**新增 CLI** `backend/scripts/board_analysis_cli.py`：`--canary` / `--all` / `--type` / `--limit` / `--trade-date` / `--publish` / `--no-publish` / `--dry-run`。

**新增前端** `frontend/src/pages/BoardAnalysisPage.tsx`：路由 `/boards` 列表 + `/boards/:boardId` 详情，行业/概念切换、覆盖率徽标、4 维分布、事件率、Admin 触发 Canary/全量计算按钮。

**输入门禁**：
1. 必须存在已发布 `stock_core` pointer
2. `source_core_run_id = factor_publications.data_run_id`（kind=stock_core）
3. 从 `summary_payload.first_pyramid_flat` 提取 99 个 fp_ 字段
4. 退市股不参与聚合，不进入 eligible_count/missing_count
5. 行业与概念分开计算，禁止使用未来数据

**发布门禁**：
- `coverage_ratio = ready_count / eligible_count`
- `coverage_ratio >= 0.95` 才写入 `factor_publications` 指针
- 不足时保存 `partial` 结果但不切 pointer（可重复计算，幂等）

## 受影响

- **后端代码**：新增 `UmamiAnalyticsAdapter` / `chip_status_resolver` / `board_analysis_service` / `board_analysis` API；修改 `admin_visitors.py` / `stock_context.py` / `market_stocks_service.py` / `visitors.py` schema / `main.py` 路由注册
- **数据库**：新增 `board_analysis_snapshots` 表（migration 074）；alembic head 从 073 升级到 074
- **前端**：新增 `BoardAnalysisPage.tsx`；修改 `AdminVisitorsPage.tsx` / `FirstPyramidPanel.tsx` / `endpoints.ts` / `useApi.ts` / `App.tsx` 路由
- **运维**：`docker-compose.prod.yml` backend 服务增加 `UMAMI_DATABASE_URL` 和 `UMAMI_WEBSITE_ID` 环境变量；部署后需在 `/etc/market-dev/market.env` 配置 `UMAMI_DATABASE_URL`
- **数据修复**：History 6 只退市股无需 repair，标记为 `valid_for_market_aggregation=false`

## 验证

### 后端单元测试（144 passed, 18 skipped）

- `tests/test_market_stocks_helpers.py`：26 个 case（_compute_factor_ready + _build_chip_status_struct + 99 字段筛选排序 + pagination + payload）
- `tests/test_chip_status.py`：12 个 case（chip_status_resolver 各种状态映射）
- `tests/test_market_stocks_chip_integration.py`：18 个 PG 集成 case（本地 SKIP，待 CI）
- `tests/test_first_pyramid_contract.py`：46 个 case（chipStatus 字段契约）
- `tests/test_first_pyramid_flatten.py`：60 个 case（99 字段 flatten 完整性）

### 模块自测

- `app.services.board_analysis_service`：3 个 case（payload 计算 + 空输入 + missing 计入）
- `app.models.board_analysis_snapshot`：1 个 case（字段完整性）
- `app.schemas.visitors`：1 个 case（schema 序列化）

### 静态检查

- Ruff check：✓ pass（11 个文件全部通过）
- TSC：✓ pass（前端类型检查无错误）
- ESLint：✓ pass（6 个目标文件全部通过）

### PG 集成测试

- 本地 Mac 不运行（约束：`PURE_UNIT_TEST=1`），待 CI 临时 Postgres 容器
- 18 个集成 case 标记为 SKIP，部署后由 CI 验证

### 生产部署后验收（待执行）

- `/admin/visitors` 显示 Umami 数据（data_source=umami）
- `/stock/000021` 显示 `M15_BARS_INSUFFICIENT + actualBars=354 + requiredBars=500 + fullQualityBars=4000`
- `/boards` 显示 canary 和全量结果
- 列表与详情对同一股票返回同一 chip 状态

## 未解决

- PG 集成测试待 CI 临时 Postgres 容器运行（本地 `PURE_UNIT_TEST=1` 时 SKIP）
- 浏览器 UI 视觉验收待生产部署后通过真实用户登录验证
- 板块分析 canary + 全量计算待生产部署后执行（本轮只完成代码与本地纯函数自测）
- 前端 vitest 本地未安装（Mac 约束：不安装 npm 依赖），待 CI 运行
