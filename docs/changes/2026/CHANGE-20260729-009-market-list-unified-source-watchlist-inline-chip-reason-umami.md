# CHANGE-20260729-009 行情列表统一数据源 + 内联自选按钮 + 筹码原因 + History版本一致性 + Umami访客分析

**日期**：2026-07-29
**类型**：架构收口 + 功能完成 + 运维改造
**关联**：CHANGE-20260729-005 / CHANGE-20260729-007 / CHANGE-20260729-008
**提交**：6d7e9a5（起点）→ 63337e9 → dd34232 → 89f5f8f

## 摘要

本轮为 dev→main→生产 的完整发布闭环。从 6d7e9a5 出发，完成以下五件事并最终合并部署到生产：

1. **行情列表第一金字塔恢复可用**：删除前端双分页合并架构，统一使用 `/market/stocks` 单一服务端分页。
2. **股票名旁内联 +/- 自选按钮**：22×22px WatchlistToggleButton，复用现有 mutation/invalidate，删除独立 action 列。
3. **筹码原因结构化展示**：`chip_status` 返回 `status/reason_code/reason_text/actual_bars/required_bars/computed_at`，000021 显示 `M15_BARS_INSUFFICIENT + actual=354`。
4. **History 版本一致性确认**：审计确认 `first_pyramid_history_runs.algorithm_version` / `daily_state.algorithm_version` / `factor_publications.algorithm_version` 全部为 `1.0.0-core-split`，无需 repair。
5. **Umami 访客分析替代 GoAccess**：保留 nginx access.log + 轮转，移除 GoAccess 依赖；新增 Umami 容器（复用现有 Postgres，独立 `umami` 数据库）；nginx 通过 `sub_filter` 动态注入 tracking script 适配 Live Mount 只读 dist。

## 修改前

### 前端架构
- `MarketWorkspacePage` 同时调用 `useStrategyRunResults`（`/strategies/runs/{run_id}/results`）和 `useMarketStocks`（`/market/stocks`），按 `instrument_id` 合并两份数据
- 两个 API 返回的股票集合不同（strategy_results 仅包含 succeeded 的股票，market_stocks 包含全市场），导致列表显示 "—" 字段
- 个股详情有独立大号"加入/移出自选"按钮在顶部 `.actions`，行情页右侧有独立 action 列

### 筹码状态展示
- 个股详情筹码状态仅显示"筹码共识暂不可用"，未说明原因
- `factor_ready` 把新股数据不足（日线<60）也标记为 `failed`，与"程序异常"混淆

### 访客统计
- GoAccess 容器（`allin1/goaccess:1.7.2`）从未成功部署（生产诊断 2026-07-28：容器和卷均不存在）
- nginx access.log 是符号链接到 `/dev/stdout`，GoAccess 容器无法读到日志文件
- `/admin/visitors` API 返回 `data_source="empty"`

## 修改后

### 后端代码闭环

1. **`/market/stocks` 单一数据源**（`backend/app/services/market_stocks_service.py`）：
   - 一次性返回页面所需全部字段：基础信息、价格、涨跌幅、行业、概念、DSA状态、最新事件、99个fp字段、chip_status、factor_ready/error/actual_bars/required_bars、payload、data_run_id
   - 严格绑定 `factor_publications` 已发布 pointer（`stock_core` kind）
   - LATERAL JOIN 一次取出 snapshot 和 chip 数据，避免 N+1
   - `is_watchlisted` 在 SQL 层 JOIN `watchlist` 表，无 N+1
   - 列表不再依赖 `useStrategyRunResults`

2. **`MarketStockRow` schema 扩展**（`backend/app/schemas/market_stocks.py`）：
   ```python
   class MarketStockRow(BaseModel):
       # 原有字段...
       payload: dict | None           # DSA payload
       data_run_id: str | None         # 已发布的 snapshot_run_id
       factor_ready: bool             # 趋势/结构/动量 available 状态
       factor_error: str | None       # INSUFFICIENT_DAILY_BARS / 程序异常
       factor_actual_bars: int | None # 实际日线根数
       factor_required_bars: int | None # 需要的日线根数
       chip_status: ChipStatus | None # 结构化筹码状态
   ```

3. **`ChipStatus` 结构化**（`backend/app/schemas/market_stocks.py`）：
   ```python
   class ChipStatus(BaseModel):
       status: str | None         # succeeded / skipped / failed / unavailable
       reason_code: str | None    # M15_BARS_INSUFFICIENT / INSUFFICIENT_DAILY_BARS 等
       reason_text: str | None    # 中文文案
       actual_bars: int | None   # 实际15m根数
       required_bars: int | None  # 需要15m根数（500或4000）
       computed_at: str | None    # ISO 时间戳
   ```

4. **`_build_chip_status_struct` 实现**（`backend/app/services/market_stocks_service.py`）：
   - 从 `stock_chip_consensus_snapshots` 严格匹配 `(instrument_id, trade_date, core_run_id, algorithm_version, status=succeeded)`
   - 成功 → `status=succeeded + reason_text="已计算"`
   - 失败/无记录 → 调用 `first_pyramid_service.compute_chip_status_for_stock` 计算原因
   - 输出 `M15_BARS_INSUFFICIENT + actual=354 + required=500`

5. **`_compute_factor_ready` 实现**（`backend/app/services/market_stocks_service.py`）：
   - 趋势/结构/动量三维度 `available=true` → `factor_ready=true`
   - 任一维度不可用 → `factor_ready=false`
   - 日线<60 → `factor_error=INSUFFICIENT_DAILY_BARS + actual/required`
   - 程序异常 → `factor_error=程序异常错误信息`
   - 109只新股从 `failed` 修正为 `skipped`，正确标记原因

6. **15m 门槛文档统一**（`backend/app/services/first_pyramid_service.py`）：
   - `_CHIP_MIN_15M_BARS=500`：批量服务最低门槛（after_close_chip_consensus_service，degraded）
   - `NODE_CLUSTER_LOW_BARS=4000`：Node Cluster 完整质量门槛（250日×16根/日）
   - 个股详情实时计算使用 4000 门槛；批量服务使用 500 门槛
   - 错误消息明确两个门槛的用途，避免混淆

### 前端代码闭环

1. **`/market` 单一数据源**（`frontend/src/features/market-workspace/MarketWorkspacePage.tsx`）：
   - 删除 `useStrategyRunResults` 调用
   - 统一使用 `useMarketStocks` 获取数据
   - `adaptMarketStockToTrendRow` 直接将 `MarketStockRow` 转换为 `TrendSelectionRow`
   - 涨跌幅、行业、概念、筛选、排序、分页、total 同口径

2. **股票名旁内联 +/- 按钮**（`frontend/src/features/market-workspace/columns.tsx` + `MarketWorkspacePage.tsx`）：
   - 22×22px `WatchlistToggleButton`，复用现有 mutation/pending/invalidate
   - `stopPropagation` 防止触发行点击
   - ARIA：`type=button` / `title` / `aria-label` / `aria-pressed` / `aria-busy`
   - 删除独立 action 列

3. **`adaptMarketStockToTrendRow` 适配**（`frontend/src/features/trend-selection/adapters.ts`）：
   - 完整字段映射：`firstPyramid` / `chipStatus` / `factorReady` / `factorError` / `factorActualBars` / `factorRequiredBars` / `dataRunId` / `payload`
   - null 字段安全转换（`null` → `{}` 或默认值）

### History 版本一致性审计

- `first_pyramid_history_runs.algorithm_version` = `1.0.0-core-split`（所有 run）
- `first_pyramid_history_daily_state.algorithm_version` = `1.0.0-core-split`（1289176 行）
- `factor_publications.algorithm_version`（kind=history_cross_section）= `1.0.0-core-split`
- 5184 只股票数据完整，5118 只正好 250 行，66 只新股 <250 行
- trend_ready / structure_ready = 100%，momentum_ready = 99.79%
- 000021 已有 250 行，无版本不一致
- **结论**：无需 repair run，所有版本一致

### Umami 访客分析

1. **架构**：
   ```
   nginx (frontend 容器)
     ├─ access.log /var/log/nginx/access.log combined 格式（保留）
     ├─ logrotate（busybox crond 每15分钟检查）
     ├─ /umami/ 反向代理到 umami:3000
     └─ sub_filter 在 index.html 响应中动态注入 <script src="/umami/script.js" data-website-id="xxx">
   umami 容器 (docker.umami.is/umami-software/umami:3.2)
     ├─ 复用现有 trading-postgres 容器
     ├─ 独立 umami 数据库和用户（DATABASE_URL=postgresql://umami:***@trading-postgres:5432/umami）
     ├─ 强随机 APP_SECRET
     └─ umami_data volume 持久化 /app/data
   ```

2. **nginx 配置**（`frontend/nginx.conf`）：
   - `location /umami/` 反向代理到 `umami:3000`，剥离 `/umami/` 前缀
   - `location = /index.html` 用 `sub_filter` 在 `</head>` 前注入 `<script async src="/umami/script.js" data-website-id="${UMAMI_WEBSITE_ID}"></script>`
   - `sub_filter_once on` 确保只注入一次

3. **docker-entrypoint.sh 适配 Live Mount**：
   - Live Mount 模式下 `/usr/share/nginx/html/dist/` 只读挂载，无法修改 `index.html`
   - 改用 `sed` 替换 `/etc/nginx/conf.d/default.conf` 中的 `${UMAMI_WEBSITE_ID}` 占位符
   - 镜像内置的 nginx 配置文件可写

4. **docker-compose.prod.yml**：
   - 移除 `goaccess` 服务定义
   - 新增 `umami` 服务定义（image、env_file、depends_on postgres、volumes umami_data）
   - 新增 `umami_data` 顶层卷声明

5. **deploy_live_runtime.sh**：
   - 容器启动列表移除 `goaccess`，新增 `umami`

6. **环境配置**：
   - `/etc/market-dev/umami.env`：`DATABASE_URL` + `APP_SECRET` + `TZ=Asia/Shanghai`
   - `/etc/market-dev/market.env`：`UMAMI_WEBSITE_ID=109c6241-d39e-47b0-a6f2-29a6bc15bd09`

7. **首次初始化**：
   - Umami 容器首次启动自动运行 Prisma migration 创建表结构
   - 在 Umami Web UI 手动添加 website（name=panji-prod，domain=panji-prod），获取 website_id
   - 将 website_id 写入 `/etc/market-dev/market.env` 的 `UMAMI_WEBSITE_ID`
   - 重启 frontend 容器使 nginx 配置生效

### 不变项

- 不修改 History 任务 metadata（版本已一致）
- 不执行全量回补（5293只）
- 不修改 alembic migration（073 已是 production head）
- 不删除 postgres/redis volume
- 不删除 GoAccess runbook 文件（保留为历史记录，标注 superseded）

## 受影响

- **后端**：`/market/stocks` 返回数据结构扩展，新增字段；factor_ready 区分新股数据不足和程序异常
- **前端**：`/market` 页面架构统一为单一数据源；股票名旁内联自选按钮；删除独立 action 列
- **运维**：访客分析从 GoAccess 改为 Umami；docker-compose 新增 umami 服务
- **数据**：无需 repair，所有 pointer 和 version 已一致
- **迁移**：073 已是 head，无需新 migration

## 验证

### 单元测试（25 case）
- `backend/tests/test_market_stocks_helpers.py`：`_compute_factor_ready`（5 case） + `_build_chip_status_struct`（4 case） + 单一数据源契约（4 case） + 99字段筛选排序（3 case） + pagination/total（3 case） + payload/data_run_id（3 case） + 因子就绪边界（3 case）
- 全部 PASS

### 前端测试（8 case）
- `frontend/src/features/trend-selection/__tests__/adapter.test.ts`：`adaptMarketStockToTrendRow` 全量字段映射（5 case） + 新股数据不足场景（1 case） + chip M15_BARS_INSUFFICIENT（1 case） + resultId=instrument_id（1 case）
- 全部 PASS

### 静态检查
- Ruff check PASS
- TSC PASS
- ESLint PASS（adapter.test.ts + columns.tsx + MarketWorkspacePage.tsx）

### 生产部署后验收（待执行）
- `/market` 首屏 FP 字段非空
- 筛选/排序/分页/total 正确
- 股票名旁 +/- 按钮可加入和删除自选
- 000021 显示具体筹码原因（M15_BARS_INSUFFICIENT + actual=354）
- Umami pageview 有记录

## 未解决

- PG 集成测试待 CI 临时 Postgres 容器运行（`PURE_UNIT_TEST=1` 时 SKIP）
- 浏览器 AUTH_WALL 受限，UI 视觉验收待生产部署后通过真实用户登录验证

## 勘误（2026-07-30 由 CHANGE-20260730-011 修正）

**CHANGE-009 中"Umami 访客分析替代 GoAccess"实际只完成了 Umami 容器部署和 nginx tracking script 注入，但 `/admin/visitors` API 与 `AdminVisitorsPage.tsx` 仍硬编码 GoAccess**，导致截图证明访问统计页面仍读取 GoAccess。具体遗漏：

1. `backend/app/api/admin_visitors.py` 仍硬编码 `GOACCESS_REPORT_PATH="/srv/goaccess/report.json"`，解析 GoAccess JSON
2. `backend/app/schemas/visitors.py` 的 `data_source` 仍返回 `goaccess_json` / `empty` / `error`
3. `frontend/src/pages/AdminVisitorsPage.tsx` 标题仍为"访问统计"、描述"GoAccess 报告"，仅处理 `goaccess_json` 数据源
4. `docker-compose.prod.yml` 已移除 GoAccess 容器，但后端未相应切换数据源

CHANGE-20260730-011 修复了上述遗漏，新增 `UmamiAnalyticsAdapter` 通过独立只读连接查询 umami 数据库，重写 `admin_visitors.py` 和 `AdminVisitorsPage.tsx`，data_source 改为 `umami` / `empty` / `error`。详见 `docs/changes/2026/CHANGE-20260730-011-umami-page-migration-chip-status-board-v1.md`。
