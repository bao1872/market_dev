# CHANGE-20260728-004：本地数据架构纠正 + 永久禁用测试库 + de7fbcb 遗留修复

状态：进行中（代码+目标测试通过，浏览器真实链路验收待完成）
日期：2026-07-28
类型：architecture + bugfix
领域：运行体系 / 权限 / 量化模型 / 安全

相关 PRD：

- `../../prd/80-system-runtime.md`：SR-03（共享 PostgreSQL）、SR-40（Scheduler/Worker 隔离）
- `../../prd/60-permissions-admin.md`：PA-03（30 天周期有效期）
- `../../prd/20-quant-model.md`：QM-01~QM-43（第一金字塔定稿）

相关 Maps：

- `../../maps/80-system-runtime.md`：SR-03 本地数据源、SR-40 进程隔离
- `../../maps/60-permissions-admin.md`：PA-03 邀请码 30 天周期
- `../../maps/20-quant-model.md`：第一金字塔结构维度契约

相关 Rules：

- `../../../AGENTS.md`：§8 基础安全边界（本地数据源、测试库禁止、Owner 账户保护）
- `../../../rules/30-access-security.md`：受保护 Owner 账户

相关提交或 PR：

- 基线：de7fbcb（CHANGE-20260728-003）
- 本轮 commit：待填写

替代：

- 部分修正 CHANGE-20260728-003 中"本地 Backend 使用 bz_stock_test + Redis DB15"的临时方案

被替代：

- 无

## 1. 摘要

本轮在 dev 分支完成四项紧密关联的纠正与修复：(1) 永久禁用本地测试库 `bz_stock_test`，
本地 Backend 固定连接正式 `bz_stock` 数据源；(2) 修复邀请码 `grant_days` 兼容路径
（旧邀请码保留天数计算）；(3) 完成第一金字塔 `active_ob_count` 按 `not mitigated` 统计、
状态文字中文化、前端显示主要/短线结构方向和事件级别；(4) 修复 `StrategyDataTable`
URL 同步导致的 "Maximum update depth" 无限循环。

## 2. 背景与问题

- **数据架构错误**：de7fbcb 提交中本地 Backend 仍连接 `bz_stock_test`（APP_ENV=test），
  违反"本地必须使用正式数据源"原则；`.env.test` 存在且强制 DB15。
- **邀请码兼容缺失**：de7fbcb 的 `_compute_expires_at` 始终调用
  `_compute_expires_at_from_months`，忽略旧邀请码 `grant_days`（天数），导致旧邀请码
  兑换后到期时间计算错误。
- **active_ob_count 字段错误**：`first_pyramid_service` 使用 `ob.get("is_active", False)`
  统计活跃 OB，但 SMC 输出中不存在 `is_active` 字段，应使用 `not mitigated`。
- **状态文字非中文**：结构维度 statusText 使用 "Swing"/"Internal"，违反纯中文要求。
- **前端缺失显示**：FirstPyramidPanel 未显示主要/短线结构方向、事件级别（主要级别/短线级别），
  事件名使用英文（BOS/CHoCH/OB_ENTRY/EQH/EQL）。
- **Maximum update depth**：`StrategyDataTable` URL 同步 useEffect 将 `searchParams` 放入
  依赖且调用 `setSearchParams`，导致 searchParams→setSearchParams→searchParams 无限循环。

## 3. 变化前

- 本地 Backend：APP_ENV=test，DATABASE_URL=bz_stock_test，.env.test 存在。
- `subscription_service._compute_expires_at`：始终调用 `_compute_expires_at_from_months`，
  不检查 `grant_days`。
- `first_pyramid_service._build_structure_dimension`：`active_ob_count` 使用
  `sum(1 for ob in order_blocks if ob.get("is_active", False))`；statusText 使用
  "Swing"/"Internal"。
- `FirstPyramidPanel.tsx`：未显示 `swing_direction`/`internal_direction`；事件名使用英文；
  未显示 `structure_level`。
- `StrategyDataTable.tsx`：URL 同步 useEffect 无变更检测，导致无限循环。
- `conftest.py`：纯单元测试必须 APP_ENV=test 且连接数据库，无法跳过 DB 初始化。

## 4. 变化内容

### 4.1 永久禁用测试库 + 切换正式数据源

- 删除 `backend/.env.test`（gitignored 本地测试配置）。
- 删除 `bz_stock_test` 中 email=8752028@qq.com 的测试复制账户及其 user_roles（事务删除）。
- 本地 Backend 使用 `APP_ENV=development` + `backend/.env` 启动，DATABASE_URL=bz_stock。
- `backend/scripts/verify_quote_trustworthy.py` 移除 `bz_stock_test` 引用，明确 CI 临时库约束。
- AGENTS.md §8 新增四条硬规则：本地固定 bz_stock、禁止持久测试库、禁止本地后台任务、
  禁止创建测试数据/写 Owner 密码。
- `docs/runbooks/local-development.md` 新增"核心数据架构规则"小节，更新安全边界。

### 4.2 邀请码 grant_days 兼容修复

- `subscription_service._compute_expires_at(base, invite)` 改为优先检查 `grant_months`，
  其次 `grant_days`，最后默认 30 天。
- 新增 `test_invite_code_30day_period.py` 中 `_MockInvite` 替身与 7 个兼容性测试
  （grant_months 优先、grant_days 兼容、默认 30 天、跨月、跨年、非法值）。
- 不追溯修改已有邀请码记录。

### 4.3 第一金字塔 active_ob_count + 中文化 + 前端显示

- `first_pyramid_service._build_structure_dimension`：
  - `active_ob_count` 改为 `sum(1 for ob in order_blocks if not ob.get("mitigated", False))`。
  - `statusText` 改为纯中文："主要结构偏多/偏空/未形成" + "短线结构偏多/偏空/未形成"。
- `frontend/src/features/stock-research/FirstPyramidPanel.tsx`：
  - 新增 `EVENT_TYPE_LABEL` 中文映射（BOS=结构突破、CHoCH=结构转折、OB_ENTRY=进入订单区域、
    EQH=连续高点、EQL=连续低点）。
  - 新增 `formatStructureLevel` 函数（swing=主要级别、internal=短线级别、其他=null）。
  - 结构维度卡新增"主要结构方向"+"短线结构方向"显示。
  - 事件项显示中文事件名 + 结构级别标签。
- `test_first_pyramid_contract.py`：statusText 断言改为"主要结构"/"短线结构"。

### 4.4 Maximum update depth 修复

- `frontend/src/components/StrategyDataTable.tsx`：URL 同步 useEffect 新增变更检测，
  仅在 managed keys 实际变化时才 `setSearchParams`，防止无限循环。

### 4.5 纯单元测试跳过 DB 机制

- `backend/tests/conftest.py`：新增 `PURE_UNIT_TEST=1` 环境变量检查，
  设置时跳过 APP_ENV 校验、TEST_DATABASE_URL 校验、Alembic 迁移和 DB 初始化。
- 纯单元测试（邀请码 30 天周期计算）可在不连接任何数据库的情况下运行。

## 5. 变化后

- 本地 Backend 固定连接 `bz_stock`（8272 instruments），Redis DB15（本地隔离）。
- 永久禁止本地连接 `bz_stock_test`；`.env.test` 已删除。
- 邀请码到期时间：grant_months 优先（30×N 天），兼容 grant_days（天数），默认 30 天。
- 第一金字塔：active_ob_count 按 not mitigated 统计；statusText 纯中文；
  前端显示主要/短线结构方向、中文事件名、结构级别标签。
- StrategyDataTable 不再出现 Maximum update depth 错误。
- 纯单元测试可设 PURE_UNIT_TEST=1 跳过 DB 初始化。

## 6. 影响范围

### 用户行为

- 管理员生成的邀请码到期时间按 30 天周期计算；旧邀请码 grant_days 仍有效。
- 第一金字塔结构卡显示主要/短线结构方向（中文）+ 中文事件名 + 级别标签。
- /market 页面不再出现 Maximum update depth 错误。

### API 或契约

- `GET /api/v1/stocks/{symbol}/first-pyramid` 返回的 `structure.continuousFactors`
  `active_ob_count` 语义变化（is_active → not mitigated）；`statusText` 文字变化。
- 邀请码到期时间计算逻辑变化（兼容 grant_days）。

### 数据

- 无 schema 变化。
- `bz_stock_test` 中测试复制账户已删除（事务）；正式库 `bz_stock` 数据未修改。
- 已有邀请码不追溯修改。

### 前端

- `FirstPyramidPanel.tsx`：新增中文事件名映射、结构级别显示、主要/短线结构方向。
- `StrategyDataTable.tsx`：URL 同步新增变更检测。

### 后端

- `subscription_service.py`：`_compute_expires_at` 兼容 grant_days。
- `first_pyramid_service.py`：active_ob_count + statusText 中文化。
- `conftest.py`：PURE_UNIT_TEST=1 跳过 DB。
- `verify_quote_trustworthy.py`：移除 bz_stock_test 引用。

### 部署与运行

- 本地开发阶段；不部署腾讯云。

## 7. 迁移与兼容

- 无 Migration。
- `active_ob_count` 语义变化：旧消费者应意识到这是 not mitigated 统计（更准确）。
- `grant_days` 字段保留兼容路径，旧邀请码不追溯修改。
- `statusText` 文字变化：旧消费者应适配中文"主要结构"/"短线结构"。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 数据库连接 | SELECT current_database() | PASS | DB=bz_stock, instruments=8272 |
| Redis 连接 | PING | PASS | Redis DB=15, PING=True |
| 邀请码 30 天周期 | `test_invite_code_30day_period.py` 纯单元测试 | PASS | 默认/N周期/跨月/跨年/grant_days兼容/非法值 |
| 第一金字塔契约 | `test_first_pyramid_contract.py` | PASS | statusText 中文化断言 |
| Ruff | 修改的 Python 文件 | PASS | All checks passed |
| TSC | 前端全量 | PASS | 无错误 |
| ESLint | 修改的前端文件 | PASS | 0 错误 |
| 浏览器真实链路 | /market → 股票详情 → 第一金字塔 | 待验收 | 服务运行中 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| AGENTS.md | §8 新增四条硬规则（本地数据源、测试库禁止、后台任务禁止、Owner保护） |
| docs/runbooks/local-development.md | 新增"核心数据架构规则"小节；更新安全边界 |
| docs/maps/80-system-runtime.md | SR-03 更新为 bz_stock 正式库；核验日期 2026-07-28 |
| docs/maps/60-permissions-admin.md | PA-03 更新为 grant_days 兼容 |
| docs/maps/20-quant-model.md | 第一金字塔结构维度契约更新（active_ob_count/statusText/事件级别） |
| docs/changes/INDEX.md | 新增 CHANGE-20260728-004 |

## 10. 回滚方案

- 代码回滚：还原 `_compute_expires_at` 为始终调用 `_compute_expires_at_from_months`；
  还原 `active_ob_count` 为 `is_active`；还原 statusText 为英文；
  还原 FirstPyramidPanel 事件名为英文；还原 StrategyDataTable URL 同步逻辑；
  还原 conftest.py 纯单元测试机制。
- 数据：`bz_stock_test` 中已删除的测试账户无法恢复（仅为上轮创建的复制账户）。
- 配置：如需恢复测试库连接，需重新创建 `.env.test`（但违反新规则，不推荐）。

## 11. 遗留问题与风险

- 浏览器真实链路验收（/market → 股票详情 → 第一金字塔）待完成。
- `bz_stock_test` 数据库实体仍存在于 PostgreSQL 中（仅删除了本地连接入口和测试账户），
  未来可考虑在服务器侧清理。

## 12. 后续变化

- 无。
