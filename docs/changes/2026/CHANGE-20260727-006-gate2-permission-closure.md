# CHANGE-20260727-006：Gate 2 PRD60 权限闭环（require_any_capability + 邀请码三勾选 + per-capability 管理 + UI gating）

状态：已完成（代码+测试+lint 已核验；三进程真实页面验证因 swap 阈值延后）
日期：2026-07-27
类型：behavior
对应 PRD：`docs/prd/60-permissions-admin.md`
对应 Map：`docs/maps/60-permissions-admin.md` §12

## 1. 变更摘要

Gate 2 在 Phase 5B-2 capability 模型（CHANGE-20260727-005）基础上完成 PRD60 权限产品闭环：

1. **`require_any_capability`**：`/market` 路由允许 `self_selection` 或 `market_data` 任一进入（PA-10/PA-11）。
2. **`require_watchlist_limit`**：watchlist 数量上限优先从 `self_selection` capability 取值，不再从 legacy plan limits 取值（PA-02）。
3. **邀请码弹窗三勾选**：取消"套餐类型"主入口，改为 `self_selection`/`market_data`/`research_replay` 三项勾选；`self_selection` 必填 `watchlist_limit`（PA-20）。
4. **管理员 per-capability 管理**：用户抽屉直接查看/授予/撤销/修改三类 capability 及各自 `expires_at`/`watchlist_limit`。
5. **前端 UI gating**：仅 `self_selection` 用户详情按钮禁用；仅 `market_data` 用户隐藏自选 scope 与操作列。

## 2. 背景

Phase 5B-2 落地了 `UserCapability` 模型与 `require_capability` 单一权限检查，但未完成产品闭环：
- `/market` 仅允许 `self_selection` 进入，`market_data` 用户无法查看行情列表。
- `watchlist_limit` 仍从 legacy `plan_code` limits 取值，未切换到 capability。
- 邀请码弹窗仍以"套餐类型"为主入口，无法组合 capability。
- 管理员只能改套餐，无法 per-capability 授予/撤销。
- 前端无 UI gating，`self_selection` 用户点击详情按钮跳转后被 API 403 拒绝（体验差）。

## 3. 变化内容

### 3.1 后端新增

| 文件 | 新增 |
|---|---|
| `access_control_service.py` | `require_any_capability(*capabilities)`、`require_watchlist_limit()` |
| `admin_subscription.py` | `GET/POST/DELETE /admin/users/{user_id}/capabilities` |
| `subscription_service.py` | `grant_capability_to_user`、`revoke_capability_from_user`、`get_user_capabilities`、`list_subscribers_with_capabilities` |
| `schemas/subscription.py` | `CapabilityInfoResponse`、`GrantCapabilityRequest`、`UserCapabilitiesResponse`、`RevokeCapabilityRequest`；`MemberListItem` 新增 `capabilities` 字段 |

### 3.2 后端调用点变更

| 文件 | 变更 |
|---|---|
| `api/market.py` | `require_capability("self_selection")` → `require_any_capability("self_selection", "market_data")` |
| `api/watchlist.py` | `require_quota("monitor_limit")` → `require_watchlist_limit()` |

### 3.3 前端新增

| 文件 | 新增 |
|---|---|
| `App.tsx` | `CapabilityAnyRoute` 组件（任一 capability 通过即放行） |
| `api/endpoints.ts` | `getUserCapabilities`、`adminGrantCapability`、`adminRevokeCapability` + TS 类型 |
| `hooks/useApi.ts` | `useUserCapabilities`、`useAdminGrantCapability`、`useAdminRevokeCapability` |

### 3.4 前端 UI 变更

| 文件 | 变更 |
|---|---|
| `App.tsx` | `/market` 路由守卫改用 `CapabilityAnyRoute(['self_selection','market_data'])` |
| `AdminUsersPage.tsx` | 邀请码弹窗：套餐选择 → 三勾选 + `watchlist_limit`；用户抽屉新增"权限"tab |
| `MarketWorkspacePage.tsx` | `canAccessStockDetail`/`canAccessWatchlist` 控制 `onNavigateToStock`/`onToggleWatchlist` 传递；无自选权限时移除操作列 |
| `MarketToolbar.tsx` | `canAccessWatchlist` prop 控制"自选"scope 按钮可见性 |
| `StockDetailPage.tsx` | 来源徽章统一使用 `sourceCtxV2.origin`（Gate 1 修复） |

### 3.5 测试

- 新增 `backend/tests/test_gate2_capability_schemas.py`（35 tests，纯单元测试不依赖 DB）
- 覆盖：schema 校验、`require_any_capability` 权限矩阵、`require_watchlist_limit` 来源优先级、邀请码 capability 组合

## 4. 权限矩阵

| Capability | /market | /stock/:symbol | /watchlist | /replay |
|---|---|---|---|---|
| self_selection only | YES | 403（按钮禁用） | YES | 403 |
| market_data only | YES | YES | 403（隐藏） | 403 |
| research_replay only | 403 | 403 | 403 | YES |
| all three | YES | YES | YES | YES |
| admin | YES | YES | YES | YES |
| none | 403 | 403 | 403 | 403 |

## 5. 兼容性

- 旧 `plan_code` fallback 仅兼容无 `user_capabilities` 行的用户，不覆盖已有独立授权。
- `require_feature` / `require_quota` 保留兼容期，未被删除。
- 旧邀请码（`plan_code` 模式）仍可兑换，兑换时按 fallback 推断创建 capability 行。

## 6. 验证

- Ruff：通过（0 errors）
- TSC：通过（0 errors）
- ESLint：通过（0 errors，4 pre-existing warnings）
- pytest：35/35 通过
- 三进程真实页面/API 验证：未执行（swap 较起点 +1071MB 超阈值，按约束停止重任务）

## 7. 未完成项（延后）

- Gate 3：第一金字塔完整契约与右侧 UI
- Gate 4：盘后编排容错 / 15:05 调度
- Gate 5：Worker 心跳展示 / GoAccess 管理员访客分析
- 三进程真实页面/API 验证（swap 阈值解除后补做）
