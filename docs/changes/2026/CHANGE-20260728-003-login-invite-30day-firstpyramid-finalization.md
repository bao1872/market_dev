# CHANGE-20260728-003：本地登录恢复 + 邀请码 30 天周期 + 第一金字塔定稿

状态：进行中（代码+目标测试通过；真实浏览器验收待启动服务后完成）
日期：2026-07-28
类型：behavior
领域：权限 / 量化模型 / 安全
负责人：待填写

相关 PRD：

- `../../prd/60-permissions-admin.md`：PA-03（30 天周期有效期）
- `../../prd/20-quant-model.md`：QM-01~QM-43、QM-60~QM-62（第一金字塔定稿）

相关 Maps：

- `../../maps/60-permissions-admin.md`：PA-03 实现入口
- `../../maps/20-quant-model.md`：SMC 结构方向 + 第一金字塔结构维度

相关 Rules：

- `../../../rules/30-access-security.md`：受保护 Owner 账户
- `../../../AGENTS.md`：基础安全边界（Owner 账户保护）

相关提交或 PR：

- 待填写（本轮 commit）

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮在 dev 分支完成三项紧密关联的收口工作：(1) 恢复本地登录并在 AGENTS/rules 中
固化 Owner 账户保护硬规则；(2) 将邀请码有效期从自然月（relativedelta）改为固定
30 天周期（1 周期=30 天，N 周期=N×30 天）；(3) 第一金字塔定稿——结构维度同时
输出 swing_direction（主要）与 internal_direction（短线），BOS/CHoCH/OB 事件
显式标注 structure_level，EQH/EQL 事件 structure_level=null。

## 2. 背景与问题

- 本地登录失效：bz_stock_test 中无 8752028@qq.com 账户，无法本地验收。
- 邀请码有效期用 `relativedelta(months=N)` 计算，跨月天数不一致（1/31 + 1 月 = 2/28
  或 3/1），与"固定周期"语义不符。
- 第一金字塔结构维度只输出 `swing_bias`，未输出 `internal_direction`；BOS/CHoCH/OB
  事件未显式标注 swing/internal 级别，违反定稿要求。
- 上轮 sourceBadge 测试用源码字符串匹配证明行为，不可靠。

## 3. 变化前

- `subscription_service._compute_expires_at_from_months` 用 `relativedelta(months=N)`。
- `smc_pine_core.compute_smc_pine` 输出 dict 含 `swing_bias`，不含 `internal_bias`。
- `first_pyramid_service._build_structure_dimension` 的 `continuousFactors` 只有
  `swing_bias`；事件 extra 不含 `structure_level`。
- AGENTS/rules 无 Owner 账户保护规则。
- `detailSourceLoadingContract.test.ts` CHANGE-005-6 用源码字符串匹配。

## 4. 变化内容

### 4.1 本地登录恢复与 Owner 账户保护

- 在 bz_stock_test 创建 8752028@qq.com 本地验收账户（admin 角色，密码哈希按项目
  `verify_password` 规则）；不复制或删除生产业务数据。
- AGENTS.md §8 基础安全边界新增硬规则：禁止修改/删除 8752028@qq.com 的
  email/password_hash/status/角色/权限/订阅；清理测试数据前必须先排除此邮箱。
- `rules/30-access-security.md` 同步新增"受保护 Owner 账户"小节。

### 4.2 邀请码 30 天周期

- `subscription_service._compute_expires_at_from_months` 改为
  `timedelta(days=30 * grant_months)`；保留 `grant_days` 兼容路径。
- `invitation.py` / `user_capability.py` / `schemas/invitation.py` /
  `admin_subscription.py` 注释和字段说明统一为"30 天周期"。
- 前端 `AdminUsersPage.tsx` 显示从"X 个月"改为"X × 30天"。
- 已有邀请码不追溯修改。

### 4.3 第一金字塔定稿

- `smc_pine_core.compute_smc_pine` 输出 dict 新增 `internal_bias`
  （取值 1/-1/0，与 swing_bias 同语义，独立计算）。
- `first_pyramid_service._build_structure_dimension`：
  - `continuousFactors` 新增 `swing_direction`（=swing_bias）和
    `internal_direction`（=internal_bias）。
  - BOS/CHoCH 事件 `extra.structure_level = "swing" | "internal"`（基于 SMC 事件
    `internal` 字段映射）。
  - OB_ENTRY 事件 `extra.structure_level = "swing" | "internal"`。
  - EQH/EQL 事件 `extra.structure_level = None`（禁止推测）。
  - `statusText` 同时体现 Swing 和 Internal 方向。
- 链路完整打通：compute → after-close (feature_snapshot_service) →
  summary_payload → StockFeatureSnapshot → GET /api/v1/stocks/{symbol}/first-pyramid
  → FirstPyramidPanel.tsx。

### 4.4 sourceBadge 行为测试

- `stockDetailNavigation.ts` 新增 `computeSourceBadge(origin, contextInvalid)` 纯函数。
- `StockDetailPage.tsx` 改用该函数。
- `detailSourceLoadingContract.test.ts` CHANGE-005-6 改为行为测试（直接 import
  并断言函数返回值），不再用源码字符串匹配。

## 5. 变化后

- 本地 Backend（bz_stock_test + Redis DB15）可用 8752028@qq.com 登录。
- 邀请码有效期统一为 30 天周期，跨月/跨年按天数计算。
- 第一金字塔结构维度同时输出主要/短线方向，事件标注结构级别。
- Owner 账户在代码层有硬规则保护。
- sourceBadge 测试为真实行为测试。

当前完整实现细节以相关 Maps 为准。

## 6. 影响范围

### 用户行为

- 管理员生成的邀请码到期时间按 30 天周期计算；UI 显示"X × 30天"。
- 第一金字塔结构卡同时显示 Swing/Internal 方向。

### API 或契约

- `GET /api/v1/stocks/{symbol}/first-pyramid` 返回的 `structure.continuousFactors`
  新增 `swing_direction`/`internal_direction`；事件 `extra.structure_level` 新增。
- 邀请码到期时间 API 返回值按 30 天周期计算。

### 数据

- 无 schema 变化；`Subscription.expires_at` 仍为 datetime，只是计算方式不同。
- 已有邀请码不追溯修改。

### 前端

- `AdminUsersPage.tsx` 邀请码列表显示"X × 30天"。
- `FirstPyramidPanel.tsx` 通过 `continuousFactors` 自动消费新字段（无代码改动）。
- `StockDetailPage.tsx` 通过 `computeSourceBadge` 显示来源徽标。

### 后端

- `subscription_service.py` / `first_pyramid_service.py` / `smc_pine_core.py` 等。
- `invitation.py` / `user_capability.py` / `admin_subscription.py` 注释更新。

### Worker 与任务

- 无影响。

### 部署与运行

- 本地开发阶段；不部署腾讯云。

## 7. 迁移与兼容

- 无 Migration。
- `swing_bias` 字段保留（与 `swing_direction` 同义），旧消费者兼容。
- `grant_days` 字段保留兼容路径。
- 已有邀请码不追溯修改。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| 邀请码 30 天周期 | `test_invite_code_30day_period.py` 7 测试 | PASS | 默认 1 周期=30 天、N 周期=N×30 天、跨月跨年按天数、边界 |
| 第一金字塔契约 | `test_first_pyramid_contract.py` 44 测试 | PASS | 含 6 个新增 structure_level 测试 |
| sourceBadge 行为 | `detailSourceLoadingContract.test.ts` CHANGE-005-6 | 待运行 | node 20 不支持 --experimental-strip-types，TSC+ESLint 通过 |
| Ruff | 7 个修改的 Python 文件 | PASS | All checks passed |
| TSC | 前端全量 | PASS | 无错误 |
| ESLint | 4 个修改的前端文件 | PASS | 0 错误（4 个预存警告与本改无关） |
| 本地登录 | API + 浏览器 | 待验收 | 服务需重启 |
| 浏览器 Console/Network | 个股详情、邀请码、第一金字塔 | 待验收 | 服务需重启 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | `prd/60-permissions-admin.md` PA-03 改为 30 天周期 |
| Maps | `maps/60-permissions-admin.md` PA-03 实现入口；`maps/20-quant-model.md` SMC 结构方向 |
| Rules | `rules/30-access-security.md` 新增受保护 Owner 账户；`AGENTS.md` §8 新增硬规则 |
| Runbooks | 无变化 |

## 10. 回滚方案

- 代码回滚：还原 `subscription_service._compute_expires_at_from_months` 为
  `relativedelta(months=N)`；还原 `smc_pine_core` 输出 dict；还原
  `first_pyramid_service._build_structure_dimension`。
- 数据：本地验收账户可删除（须先排除 8752028@qq.com）。
- 已有邀请码未追溯修改，无需回滚数据。

## 11. 遗留问题与风险

- 真实浏览器验收（登录、邀请码 30 天显示、第一金字塔四层、Console/Network）待启动
  服务后完成。
- sourceBadge 测试需 node ≥ 22 才能运行（--experimental-strip-types）；当前 node 20
  仅 TSC/ESLint 通过。

## 12. 后续变化

- 无。
