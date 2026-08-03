# 权限与管理后台 PRD

状态：已确认  
最后确认日期：2026-07-26  
对应 Map：`../maps/60-permissions-admin.md`  
需求所有权：邀请码、权限、有效期、页面能力和管理后台

## 1. 权限模型

### PA-01 三类独立权限

权限分为：

1. 自选管理，包含盘中监控；
2. 行情管理；
3. 复盘与竞价（含复盘模块与竞价分析模块，单一 capability `research_replay`）。

三类权限独立授予。

### PA-02 自选数量

授予自选管理权限时，管理员必须可以自由设置股票数量。

### PA-03 30 天周期有效期

邀请码或权限有效期按固定 30 天周期表达：1 周期 = 30 天，N 周期 = N×30 天。
起算点保留现有业务定义（注册时为 now，续期未到期时为 old_expires_at，已到期时为兑换当天）。
跨月/跨年按天数计算，不按自然月天数或 relativedelta 计算。已有邀请码不追溯修改。

## 2. 能力边界

### PA-10 自选管理

自选管理用户：

- 可查看和管理自选；
- 可使用盘中监控；
- 可见行情标签下的列表视图；
- 是否可进入个股详情由其其他权限决定。

### PA-11 行情管理

行情管理用户：

- 可查看行情能力和个股详情；
- 不自动获得自选列表；
- 不自动获得盘中监控。

### PA-12 复盘与竞价

"复盘与竞价"为单一 capability（`research_replay`，机器值不变，中文统一展示"复盘与竞价"），同时控制复盘模块与竞价分析模块：

- 复盘模块能力由 `70-review.md` 定义；
- 竞价分析模块（行情页竞价入口、`/auction`、`/auction/board/:boardId`、`/auction/stock/:symbol`）归同一 capability 控制，不引入独立 auction capability；
- 任一无 `research_replay` 权限用户，前后端均不得呈现竞价入口，直接访问 URL 由后端 `require_capability("research_replay")` 拒绝（与复盘 403 契约一致）；
- 管理员（admin）豁免，可直接访问。

### PA-13 详情访问

仅拥有自选管理权限的用户可以看行情标签下的列表视图，但不能直接进入个股详情查看明细，除非同时拥有行情管理权限。

## 3. 邀请码

### PA-20 生成

管理员生成邀请码时可选择：

- 权限组合；
- 自选股票数量；
- 有效期；
- 必要备注。

### PA-21 激活和过期

系统应明确邀请码：

- 是否可重复使用；
- 激活时间；
- 到期时间；
- 过期后的权限行为；
- 管理员撤销或修改行为。

## 4. 管理后台

### PA-30 管理能力

后台至少支持：

- 邀请码生成和查看；
- 用户权限查看和修改；
- 自选数量配置；
- 权限有效期；
- 管理调试入口；
- 必要的行情和股票管理。

### PA-31 模块边界

“自选管理”包含盘中和自选数量；“行情管理”不包含自选和盘中；“复盘管理”不提前定义未确认能力。

## 5. 验收标准

- 后端权限检查与前端可见性一致。
- 直接访问受限 URL 时仍由后端阻止。
- 权限矩阵在 Map 中与实际代码逐项对应。
- 自选数量不能仅依赖前端限制。

## 6. 权限模型 V2 统一（2026-08-03 确认）

### PV2-01 功能权限唯一真源

功能访问判权唯一依据 `user_capabilities`（三类独立 capability）。**Subscription 只记录商业周期**（注册/续期），**不参与功能判权**；**Plan 仅是销售/展示模板**，不作为运行时权限真源。运行时判权不得根据 plan_code 决定功能权限。

- `users.status`：控制是否允许登录；
- `user_capabilities`：功能访问唯一真源；
- `invite_codes.capabilities`：授权模板；
- `subscriptions`：商业周期记录；
- `plans`：展示/销售模板；
- `roles`：admin/member 身份。

统一由 `resolve_effective_access(user_id)` 解析（login/register/refresh//me/access/API guards/后台/默认路由共用），禁止各模块自行推导权限。

### PV2-02 默认入口矩阵

| 权限组合 | 默认入口 |
|---|---|
| admin | `/admin/overview` |
| 无 active capability | `/forbidden` |
| 仅 research_replay | `/review` |
| 仅 self_selection | `/market?scope=watchlist` |
| 仅 market_data | `/market` |
| self_selection + market_data（含 research_replay） | `/market` |

### PV2-03 legacy fallback 退出条件

无显式 `user_capabilities` 行的旧用户允许兼容期 plan fallback，但必须显式标记 `source=legacy_plan_fallback`，不得静默混入正常用户。迁移目标是所有用户生成显式 capability，legacy fallback 退出后删除。

### PV2-04 管理员页面展示合同

会员列表必须展示权限摘要（capabilities/active_keys/has_any_access/default_route/capability_source/nearest_expires/legacy_fallback）。抽屉默认打开"权限概览"，顺序：权限概览/账户信息/授权记录/审计。权限概览固定显示三张卡（self_selection/market_data/research_replay），含中文名/机器值/状态/granted_at/expires_at/watchlist_limit/source/reason。

文案：账户状态控制登录；功能范围和额度由当前有效权限决定。

### PV2-05 新邀请码必须显式授权

所有新邀请码必须显式包含非空 `capabilities`（禁止 null/[]）。self_selection 必须指定 `watchlist_limit`。旧 capabilities=NULL 邀请码标记"旧套餐模式"，禁止再用于新注册，不静默 fallback。
