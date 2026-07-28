# 权限与管理后台 Map

核验状态：已核验（Phase 5B-1 代码级差异审计）；Phase 5B-2 PA-01 capability 模型/Schema/API 已核验，测试矩阵部分实现
最后核验日期：2026-07-27
核验分支：dev
核验提交：54c601e（Phase 5B-1 基线）；Phase 5B-2 新增见 §11
核验范围：用户/角色/订阅/邀请码模型 + access_control_service + API 依赖 + 前端 capability 字段；Phase 5B-2 新增 UserCapability 模型 / require_capability / CapabilityRoute
对应 PRD：`../prd/60-permissions-admin.md`
事实所有权：认证、邀请码、权限数据结构、前后端检查和管理入口

> Phase 5B-1 基线：仅 admin token 通过不能写"权限符合预期"；本轮未修改权限业务代码，仅审计并重建 Maps。
> Phase 5B-2 增量：实施 §10 候选方案中的 PA-01 独立 capability 授权（见 §11）。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| PA-01 三类独立权限 | `Subscription.entitlement_snapshot` JSONB | 部分实现 | 套餐绑定，非独立 capability grants |
| PA-02 自选数量 | `entitlement_snapshot.monitor_limit` | 已实现 | `subscription.py:77` InviteCode.monitor_limit；`access_control_service.py:308 require_quota` |
| PA-03 30 天周期有效期 | `Subscription.expires_at` (datetime)；`subscription_service._compute_expires_at` 优先 `grant_months × 30 天`，兼容旧 `grant_days`（天数），两者均无默认 30 天 | 已实现 | 单一 expires_at，无 per-capability expiry；30 天周期=固定 30×月数天，不按自然月；旧邀请码 grant_days 保留兼容 |
| PA-10~13 权限矩阵 | `require_feature(feature_name)` 装饰器 | 部分实现 | `access_control_service.py:272`；按套餐 feature 检查，非 capability |
| PA-20~21 邀请码流程 | `InviteCode` 模型 + `InviteRedemption` | 已实现 | `invitation.py:37`；plan_code 快照，无 capability 组合 |
| PA-30~31 管理后台 | `/admin/*` 路由 + `require_admin` | 已实现 | `access_control_service.py:216 require_admin` |

## 2. 身份与认证

| 项目 | 当前实现 |
|---|---|
| 用户身份来源 | `User.email` + `password_hash`（bcrypt）；JWT token |
| 登录/激活入口 | `/api/v1/auth/login`；status: active/disabled/pending |
| Session/Token | JWT；不缓存订阅状态到登录态（避免漂移） |
| 管理员识别 | `Role.name == "admin"` via `user_roles` 关联表 |
| 权限缓存 | 无运行时缓存；每次请求实时计算 `get_effective_subscription_status` |

## 3. 权限数据结构

| 权限 | 存储字段/关系 | 后端检查 | 前端检查 |
|---|---|---|---|
| 自选管理 | `Subscription.plan_code` + `entitlement_snapshot.features` | `require_active_subscription` + `require_feature("trend_selection")` | 路由级隐藏，无 CapabilityRoute 守卫 |
| 行情管理 | 同上（observe_20/research_50 套餐） | `require_active_subscription` | 同上 |
| 复盘管理 | 同上 | `require_feature("trend_selection")` | 同上 |
| 自选数量 | `entitlement_snapshot.monitor_limit` | `require_quota("monitor_limit")` | 前端展示限额，无独立守卫 |
| 管理员 | `Role.name == "admin"` | `require_admin` | `/admin/*` 路由级 |

## 4. 当前权限矩阵

基于代码级审计（非运行时验证）：

| 能力 | observe_20 | research_50 | admin |
|---|---:|---:|---:|
| 查看行情列表 | ✅ | ✅ | ✅（豁免） |
| 查看自选 | ✅（monitor_limit=20） | ✅（monitor_limit=50） | ✅（无限制） |
| 进入个股详情 | ✅ | ✅ | ✅ |
| 使用盘中监控 | ✅ | ✅ | ✅ |
| 使用趋势选股 | ❌（features 不含 trend_selection） | ✅ | ✅（豁免） |
| 使用复盘 | ❌ | ✅ | ✅ |
| 访问 /admin | ❌ | ❌ | ✅ |

**注**：上述矩阵基于 `plans` 表的 `entitlement_snapshot` 推断；Phase 5B-1 未做运行时验证。

## 5. 邀请码流程

```text
管理员创建 (POST /admin/invite-codes)
→ 选择 plan_code（observe_20/research_50）
→ 写入 monitor_limit 快照（从 plans 表读取）
→ 设置 grant_months 或 grant_days
→ 用户激活 (POST /api/v1/auth/redeem)
→ 创建/续期 Subscription，写入 entitlement_snapshot
→ 记录 InviteRedemption（old/new expires_at）
→ 到期或撤销
```

**入口**：`backend/app/api/admin_invite_codes.py`、`backend/app/api/auth.py`
**存储**：`invite_codes` 表 + `invite_redemptions` 表 + `subscriptions` 表
**状态**：邀请码 status: unused/used/revoked

**已知偏差**：邀请码只绑定 plan_code，不支持 capability 组合（如"只给自选管理不给行情管理"）。

## 6. 后端与前端关系

后端是权限安全边界。前端隐藏只用于体验，不能替代后端检查。

**后端检查点**：
- `require_authenticated`：任何已登录用户
- `require_admin`：admin 角色
- `require_active_subscription`：有效订阅（admin 豁免）
- `require_feature(name)`：订阅 features 含该能力（admin 豁免）
- `require_quota(name)`：返回额度（admin=None 无限制）

**前端**：
- `frontend/src/api/endpoints.ts:241` 暴露 `capabilities: Record<string, unknown>`
- 无 `CapabilityRoute` 守卫组件
- 路由级隐藏（admin 路由仅在 admin 角色下显示）

## 7. 已知偏差

Phase 5B-1 审计发现的偏差（未修复，记录待下一阶段处理）：

1. **三类权限非独立**：PRD60 要求自选管理、行情管理、复盘管理三类独立权限，
   当前实现绑定为 `plan_code` 套餐，无法单独授予/撤销某一类。
2. **无 per-capability expires_at**：所有能力共享单一 `Subscription.expires_at`，
   PRD60 要求自然月有效期可按能力独立计算。
3. **邀请码无 capability 组合**：邀请码只能选定 `plan_code`，不能组合能力
   （如"自选管理 + 复盘管理，不含行情管理"）。
4. **watchlist_limit 语义偏差**：当前 `monitor_limit` 即 PRD60 的 `watchlist_limit`，
   命名不一致；功能等价。
5. **前端无 CapabilityRoute 守卫**：仅路由级隐藏，无组件级能力检查。
6. **直接 URL/API 后端检查依赖套餐**：通过 `require_feature` 检查 `entitlement_snapshot.features`，
   非独立 capability grants 检查。

## 8. 前端验证结果（Phase 5B-0）

**验证环境**：本地原生 Backend (port 8000) + Frontend (port 8008) + SSH 隧道；admin token；2026-07-27。

| 管理员路由 | 页面加载 | 主要 API | 数据展示 | 权限 |
|---|---|---|---|---|
| `/admin` | OK | `/admin/system-overview` 200 | 系统概览 | 管理员 |
| `/admin/users` | OK | `/admin/users` 200 | 用户列表 | 管理员 |
| `/admin/beta-applications` | OK | `/admin/beta-applications` 200、`/admin/beta-applications/stats` 200 | Beta 申请列表 | 管理员 |
| `/admin/after-close/pipeline` | OK（需参数） | `/admin/after-close/pipeline` 422（需 trade_date）、`/admin/after-close/pipeline/latest` 200 | 盘后流水线 | 管理员 |
| `/admin/jobs` | OK | `/admin/scheduler-job-runs` 200、`/admin/worker-heartbeats` 200 | 任务运行历史 | 管理员 |
| `/admin/strategies` | OK（POST only） | `/admin/strategies` 405（GET 不允许，POST only） | 策略管理 | 管理员 |
| `/admin/stocks/000001/debug` | OK | `/api/v1/admin/stocks/000001/debug` 200 | 调试面板 | 管理员 |
| `/admin/audit-logs` | OK | `/admin/audit-logs` 200 | 审计日志 | 管理员 |
| `/admin/members` | OK | `/admin/members` 200 | 会员管理 | 管理员 |
| `/admin/message-deliveries` | OK | `/admin/message-deliveries` 200 | 消息投递 | 管理员 |

**结论**：所有管理员路由均通过 admin token 验证。`/admin/after-close/pipeline` 返回 422 是参数校验预期行为，`/admin/strategies` 返回 405 是 POST-only 路由预期行为，均非阻塞。

**Phase 5B-1 补充**：admin token 通过不等于权限符合 PRD60。三类独立权限、per-capability expires_at、邀请码 capability 组合均未实现（见 §7 已知偏差）。

## 9. 关键代码入口

| 模块 | 入口 | 职责 |
|---|---|---|
| 用户/角色模型 | `backend/app/models/user.py` | User, Role, UserRole 表 |
| 订阅模型 | `backend/app/models/subscription.py` | Subscription 表（plan_code, expires_at, entitlement_snapshot） |
| 邀请码模型 | `backend/app/models/invitation.py` | InviteCode, InviteRedemption 表 |
| 访问控制服务 | `backend/app/services/access_control_service.py` | require_authenticated/admin/active_subscription/feature/quota |
| 访问审计服务 | `backend/app/services/access_audit_service.py` | 权限变更审计日志 |
| API 依赖 | `backend/app/api/dependencies.py` | FastAPI 依赖注入 |
| 前端 capability | `frontend/src/api/endpoints.ts:241` | capabilities: Record<string, unknown> |

## 10. 下一阶段权限实现方案（Phase 5B-2+ 候选）

> Phase 5B-2 已实施 §10.1 / §10.4 / §10.5 / §10.6（见 §11）。§10.2 per-capability expires_at 已实现；§10.3 watchlist_limit 字段已实现（self_selection 行）；§10.7 测试矩阵部分实现；§10.8 migration 顺序执行中。

**Phase 5B-1 仅记录方案，Phase 5B-2 已落地部分**：

### 10.1 Capability Grants 表
新增 `user_capabilities` 表：
- `user_id` (FK users)
- `capability` (ENUM: self_selection / market_data / research_replay)
- `granted_at`, `expires_at` (per-capability 独立有效期，1 周期=30 天)
- `source` (invite_code / admin_grant)
- `granted_by` (admin user_id)

### 10.2 独立 expires_at
- 每个 capability 有独立 `expires_at`
- 邀请码可指定 capability 组合与各自有效期
- 自然月计算已改为固定 30 天周期：`timedelta(days=30 * grant_months)`，与 `grant_months` 对齐（1 周期=30 天，N 周期=N×30 天）

### 10.3 watchlist_limit 独立字段
- `user_capabilities` 表新增 `watchlist_limit` 列（per-capability）
- 或在 `self_selection` capability 行存储 limit
- 弃用 `monitor_limit` 命名，统一为 `watchlist_limit`

### 10.4 邀请码 capability 组合
- `invite_codes` 表新增 `capabilities` JSONB 列
- 格式：`[{"capability": "self_selection", "months": 1, "watchlist_limit": 20}, ...]`
- 兑换时为每个 capability 创建独立 `user_capabilities` 行

### 10.5 require_capability 后端依赖
```python
def require_capability(capability: str) -> Callable:
    """检查用户是否拥有指定 capability 且未过期。"""
    # 替代 require_feature，直接查 user_capabilities 表
```

### 10.6 CapabilityRoute 前端守卫
- 新增 `frontend/src/components/CapabilityRoute.tsx`
- 读取 `capabilities` 字段，无权限时渲染 403 页面（不跳转）
- 替代当前路由级隐藏

### 10.7 角色组合测试矩阵
新增测试 `backend/tests/test_capability_matrix.py`：
- 角色组合：admin / observe_20 / research_50 / 无订阅
- capability 组合：self_selection only / market_data only / research_replay only / 全部 / 无
- 过期场景：单 capability 过期、全部过期、admin 不过期
- 邀请码兑换：单 capability、多 capability、重复兑换

### 10.8 Migration 顺序
1. 创建 `user_capabilities` 表（不破坏现有 Subscription）
2. 数据回填：从 `Subscription.entitlement_snapshot` 派生 `user_capabilities` 行
3. 切换 API 依赖：`require_feature` → `require_capability`
4. 弃用 `Subscription.entitlement_snapshot.features`（保留兼容期）
5. 前端引入 `CapabilityRoute`
6. 移除 `monitor_limit`，统一 `watchlist_limit`

## 11. Phase 5B-2 PRD60 PA-01 独立 capability 授权（已核验）

Phase 5B-2 落地 §10 候选方案中的 PA-01 三类独立 capability 授权。本节为 Phase 5B-2 增量事实，已通过代码级核验。

### 11.1 三类独立 capability

| capability 常量 | 值 | 对应 PRD60 能力 | API 守卫路由 |
|---|---|---|---|
| `CAPABILITY_SELF_SELECTION` | `self_selection` | 自选管理（含盘中监控+行情列表可见，PA-10） | `/market` |
| `CAPABILITY_MARKET_DATA` | `market_data` | 行情管理（个股详情，PA-11/PA-13） | `/stock/:symbol` |
| `CAPABILITY_RESEARCH_REPLAY` | `research_replay` | 复盘管理（PA-12） | `/replay` |

定义入口：`backend/app/models/user_capability.py`（`ALL_CAPABILITIES` 元组）。

### 11.2 UserCapability 模型（已核验）

- 表：`user_capabilities`（`backend/app/models/user_capability.py`）
- 唯一约束：`uq_user_capabilities_user_capability`（user_id, capability）
- 字段：`id` / `user_id` / `capability` / `watchlist_limit`（仅 self_selection 使用，PA-02）/ `granted_at` / `expires_at`（per-capability 独立有效期，1 周期=30 天，PA-03）/ `source`（invite_code/admin_grant/migration）/ `granted_by` / `created_at`
- Migration：`068_user_capabilities.py`（建表 + 从现有有效订阅回填）、`069_invite_code_capabilities.py`（邀请码 capabilities JSONB）

### 11.3 require_capability 后端依赖（已核验）

- 入口：`backend/app/services/access_control_service.py:413 require_capability(capability)`
- 行为：检查 `ctx.capabilities` 是否含指定 capability 且 active；admin 自动豁免（所有 capability active=True）
- 替代：`require_feature`（旧 feature 检查仍保留兼容期）
- 调用点：`market.py`（market_data）、`stock_context.py`（market_data）、`watchlist.py`（self_selection）

### 11.4 邀请码 capabilities JSONB（PA-20，已核验）

- 字段：`InviteCode.capabilities`（JSONB，`backend/app/models/invitation.py:91`，`none_as_null=True`）
- 格式：capability 组合数组，兑换时为每个 capability 创建独立 `user_capabilities` 行
- Migration：`069_invite_code_capabilities.py`

### 11.5 前端 CapabilityRoute（已核验）

- 守卫类型：`routeStructure.ts` 中 `GuardType` 新增 `'capability'`，替代旧 `'subscriber'`
- 路由守卫映射（`frontend/src/navigation/routeStructure.ts`）：
  - `/market` → capability 守卫（self_selection）
  - `/stock/:symbol` → capability 守卫（market_data）
  - `/replay` → capability 守卫（research_replay）
- 403 页面：`/forbidden`（ForbiddenPage，已登录但缺少指定 capability 时渲染，不跳转）
- 兼容重定向保留：`/overview`→`/market`、`/watchlist`→`/market?scope=watchlist`、`/screener`→`/market`

### 11.6 Fallback 推断（兼容期，已核验）

用户在 `user_capabilities` 表无记录时，按 `plan_code` 推断 capability（`068_user_capabilities.py` 回填逻辑，运行时同样适用）：

| plan_code | 推断 capability |
|---|---|
| `observe_20` | `self_selection`(watchlist_limit=20) + `market_data` |
| `research_50` | `self_selection`(watchlist_limit=50) + `market_data` + `research_replay` |

旧 `Subscription` / `plan_code` / `entitlement_snapshot` 保留兼容期，新读取优先、旧数据 fallback。

### 11.7 核验状态汇总

| 项目 | 状态 |
|---|---|
| UserCapability 模型 / Schema | 已核验 |
| require_capability API 依赖 | 已核验 |
| InviteCode.capabilities JSONB | 已核验 |
| CapabilityRoute 前端守卫 | 已核验 |
| Fallback 推断逻辑 | 已核验 |
| 角色组合测试矩阵（§10.7） | 部分实现 |

## 12. Gate 2 PRD60 权限代码增量（代码+单元测试通过，真实运行未核验，2026-07-27）

Gate 2 在 Phase 5B-2 capability 模型基础上完成权限代码改造（非"闭环"，真实运行验证待补）：`require_any_capability` 任一检查、`require_watchlist_limit` 来源切换、邀请码三勾选 UI、管理员 per-capability 管理、前端 UI gating。

### 12.1 require_any_capability（新增，代码+单元测试通过）

- 入口：`backend/app/services/access_control_service.py require_any_capability(*capabilities)`
- 行为：检查 `ctx.capabilities` 是否含任一指定 capability 且 active；admin 自动豁免
- 用途：`/market` 路由允许 `self_selection` 或 `market_data` 任一进入（PA-10/PA-11）
- 调用点：`market.py list_market_stocks`（替换原 `require_capability("self_selection")`）
- 前端对应：`App.tsx CapabilityAnyRoute` 组件（`/market` 路由守卫）

### 12.2 require_watchlist_limit（新增，代码+单元测试通过）

- 入口：`backend/app/services/access_control_service.py require_watchlist_limit()`
- 行为：返回 watchlist_limit 限额值，优先级：
  1. admin → `None`（无限制）
  2. `self_selection` capability active → 取 `capability.watchlist_limit`（PA-02）
  3. 无 capability 行 → fallback 到 `ctx.limits["monitor_limit"]`（兼容期）
  4. 都无 → 403
- 替代：旧 `require_quota("monitor_limit")`（不再直接从 legacy plan limits 取值）
- 调用点：`watchlist.py add_to_watchlist`（POST /watchlist）

### 12.3 管理员 per-capability 管理 API（新增，代码+单元测试通过）

- `GET /admin/users/{user_id}/capabilities`：查询用户三类 capability 状态
- `POST /admin/users/{user_id}/capabilities`：授予/修改 capability（取较晚 expires_at 不降权）
- `DELETE /admin/users/{user_id}/capabilities/{capability}`：撤销 capability
- Service 层：`subscription_service.py grant_capability_to_user / revoke_capability_from_user`
- Schema：`GrantCapabilityRequest`（capability + months + watchlist_limit）、`UserCapabilitiesResponse`
- 前端：`AdminUsersPage.tsx` 用户抽屉"权限"tab（查看/授予/撤销/续期）

### 12.4 邀请码弹窗三勾选 UI（新增，代码+TSC/ESLint 通过，真实 UI 未核验）

- 取消"套餐类型"作为主入口，改为三项勾选：`self_selection` / `market_data` / `research_replay`
- 选择 `self_selection` 时 `watchlist_limit` 必填（管理员自由输入 1-500）
- 统一 `grant_months` 按 30 天周期（1-36 周期，1 周期=30 天）
- 旧 `plan_code` 模式仍兼容（无 capabilities 时 fallback）

### 12.5 前端 UI gating（新增，代码+TSC/ESLint 通过，真实 UI 未核验）

- `MarketWorkspacePage.tsx`：
  - `canAccessStockDetail`（market_data 或 admin）= false 时，股票名渲染为纯文本（无按钮/箭头）
  - `canAccessWatchlist`（self_selection 或 admin）= false 时，隐藏自选 scope 按钮 + 移除操作列
  - 无自选权限时强制 scope=market（禁止 URL 直接访问 watchlist scope）
- `MarketToolbar.tsx`：`canAccessWatchlist` prop 控制"自选"scope 按钮可见性

### 12.6 权限矩阵（35 单元测试通过，非真实 API 集成）

| Capability | /market | /stock/:symbol | /watchlist | /replay |
|---|---|---|---|---|
| self_selection only | YES（列表+自选+盘中） | 403（按钮禁用） | YES | 403 |
| market_data only | YES（列表+详情） | YES | 403（隐藏） | 403 |
| research_replay only | 403 | 403 | 403 | YES |
| all three | YES | YES | YES | YES |
| admin | YES | YES | YES | YES |
| none | 403 | 403 | 403 | 403 |

测试文件：`backend/tests/test_gate2_capability_schemas.py`（35 tests，纯单元测试不依赖 DB）

### 12.7 Gate 2 核验状态

| 项目 | 状态 |
|---|---|
| require_any_capability 后端依赖 | 代码+单元测试通过，真实运行未核验 |
| require_watchlist_limit 后端依赖 | 代码+单元测试通过，真实运行未核验 |
| /market API 权限切换 | 代码+单元测试通过，真实运行未核验 |
| /watchlist API watchlist_limit 来源切换 | 代码+单元测试通过，真实运行未核验 |
| 管理员 per-capability 管理 API | 代码+单元测试通过，真实运行未核验 |
| 邀请码三勾选 UI | 代码+TSC/ESLint 通过，真实 UI 未核验 |
| 前端 UI gating（详情按钮/自选隐藏） | 代码+TSC/ESLint 通过，真实 UI 未核验 |
| 权限矩阵单元测试（35 项） | 通过（纯 schema/单元测试，非真实 API 集成） |
| Ruff / TSC / ESLint | 通过 |
| 三进程真实页面/API 验证 | 未核验（待补） |
