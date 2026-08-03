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

## 7. 权限模型 V2 后端合同（2026-08-03 确认）

### PV2-B01 统一授权生命周期

管理员 grant 与邀请码兑换统一走 `apply_capability_grant`，底层只接收确定性期限输入 `grant_days`（天数，正整数）。`months`（30 天周期）仅在调用边界由 `months * 30` 转换为 `grant_days`，底层不得混用绝对到期日、months、days。

期限算法统一为：
- active（现有 `expires_at` 晚于 now）：从当前 `expires_at` 顺延 `grant_days`。
- expired / revoked / 空 `expires_at`：从注入的 `now` 重新计算 `grant_days`。
- tombstone（`source="admin_revoke"`）重新授权：恢复真实来源并更新授予者。

`self_selection` 保持 `watchlist_limit` 额度校验；其他 capability 拒绝 `watchlist_limit` 参数。

### PV2-B02 场景化 legacy 物化

legacy Plan 推导权限的物化由调用场景显式控制，禁止无条件物化：

| 调用场景 | materialize_legacy | 说明 |
|---|---|---|
| 显式 Capability 邀请码**新用户注册** | `False` | 只授予邀请码声明的 capability，不得物化套餐推导权限 |
| 旧用户**显式邀请码续期** | `True` | 先物化完整 legacy 权限，避免只更新一项导致其他权限消失 |
| 管理员**首次 grant/revoke** | `True` | 先物化完整 legacy 权限 |

注册流程**不得**根据 Subscription 是否存在判断新旧用户，必须由调用边界显式传入 `materialize_legacy=False/True`。

### PV2-B03 统一用户行锁顺序

所有需要物化 legacy 的路径（管理员 grant/revoke、旧用户续期）必须先 `SELECT ... FOR UPDATE` 锁定目标 `User` 行，再查询 capability、物化、grant/revoke、写审计，最后提交。固定锁顺序避免管理员操作与邀请码续期之间形成反向锁依赖。

### PV2-B04 撤销 tombstone 合同

撤销不硬删除，采用 tombstone（`source="admin_revoke"`）：
- `admin_revoke` 记录无论 `expires_at` 是否在未来，`resolve_effective_access` 一律解析为 `active=False`。
- 无目标记录时创建撤销 tombstone（`granted_by` 为空）。
- 已撤销时重复撤销幂等，不重复插入。
- 已有记录时保留原 `granted_by`，不得把撤销人写入 `granted_by`。
- 撤销人 `revoked_by` 与原因写入审计快照。

### PV2-B05 同事务结构化审计

所有管理员 capability 写操作在同一事务内调用 `write_audit_log`：
- `target_type="user_capability"`
- `target_id="{user_id}:{capability}"`
- `action` 依据真实 `mutation_type` 生成（**不得把全部授权记成同一 action**）：
  - 授予：`capability.grant` / `capability.extend` / `capability.extend_and_quota_change` / `capability.regrant` / `capability.quota_change`
  - 撤销：`capability.revoke`
- `mutation_type` 精确区分（授权总会改变有效期，因此 `apply_capability_grant` **不产生纯 quota_change**）：
  - `grant`：新建授权行
  - `extend`：已有行续期（active 顺延 / expired 从 now 重算，额度不变或非 self_selection）
  - `extend_and_quota_change`：已有 `self_selection` 且额度变化（**无论此前 active 还是 expired**，因为本次同时修改了有效期与额度）
  - `regrant`：tombstone（admin_revoke）重新授权
  - `quota_change`：仅由独立入口 `change_self_selection_quota` 产生（不改变 `expires_at`）
  - `revoke`：撤销
- `before_data/after_data` 含用户、能力、状态、来源、期限、额度、`granted_by`。
- grant 的 after 快照含 `reason`；revoke 的 after 快照含 `revoked_by` 和 `reason`。
- 默认原因分别为 `admin_manual_grant` 和 `admin_manual_revoke`。
- 首次操作触发 legacy 物化时，`after_data` 含 `materialized_capabilities`（本次实际物化的 Capability 快照列表）；未物化为空列表。
- 管理员 grant/revoke/quota change 必须传真实 `request_id`（复用请求链的 `x-request-id`，禁止自行生成随机值；请求链未提供则为 `None`）。

邀请码注册和续期由现有 `InviteRedemption` 追溯，通用授权服务不私自写管理员审计。

### PV2-B05a 独立 quota change 入口

`apply_capability_grant` 每次都会顺延有效期，因此**不能**产生纯 quota_change。纯额度调整由独立服务 `change_self_selection_quota` 提供，并通过 API：

```
PATCH /v1/admin/users/{user_id}/capabilities/self_selection/quota
请求体：{ watchlist_limit, reason? }
```

合同：
- 只允许 `self_selection`。
- 必须已有显式 `UserCapability` 记录（无记录抛 `ValueError`，不自动授权）。
- **不修改 `expires_at`**。
- `revoked`（`source="admin_revoke"`）状态**不得**通过调整额度恢复，抛 `ValueError`。
- 只修改 `watchlist_limit`。
- `mutation_type="quota_change"`，审计 action 恒为 `capability.quota_change`。
- 先 `SELECT User FOR UPDATE` 锁行并物化 legacy 权限。
- 返回 before/after。

`DELETE /v1/admin/users/{user_id}/capabilities/{capability}` 接收可选 `reason` query 参数：
去空白、空字符串转 `None`、限长 500（与 Grant/Revoke 请求 reason 合同一致，PV2-B07）。

### PV2-B06 商业状态与功能权限解耦

商业订阅状态与功能权限完全解耦。新增纯商业状态解析结果（受限 `status` + 诊断原因）：

1. 无记录：`none`
2. 持久状态 revoked/cancelled：保持原状态
3. 缺少 `starts_at`：`expired/missing_starts_at`
4. 缺少 `expires_at`：`expired/missing_expires_at`
5. `starts_at` 晚于 `expires_at`：`expired/invalid_period`
6. 尚未开始：`pending`
7. 已到期：`expired`
8. 其余正常周期：`active`

异常商业周期采用 **fail-closed**，一律判 `expired` 并返回诊断原因。**三个入口共用唯一解析器 `resolve_commercial_status`**，对相同 `Subscription` 返回相同状态：

- `get_effective_subscription_status`（返回状态扩展为六态：none/pending/active/expired/revoked/cancelled）
- `list_subscribers`（`membership_status` 字段）
- `GET /admin/users/{user_id}/access-profile`（`subscription_summary.status`）

禁止任何入口自行比较 `expires_at` / 复制 active/expired 判断。权限解析、默认路由和 capability guard **不读取** 商业状态。

### PV2-B07 Grant/Revoke 请求 reason

Grant/Revoke 请求新增可选 `reason`：统一去除首尾空白、空字符串转 `None`、限制最大长度。保持既有路由兼容，尤其避免改变现有 DELETE 调用方式。

### PV2-B08 source/actor 授权来源合同

`apply_capability_grant` 公共签名只保留一个操作者字段 `actor_user_id`，消除 `granted_by` 与 `actor_user_id` 语义重叠：

- `source == "admin_grant"`：
  - `actor_user_id` **必须存在**（否则 `ValueError`）。
  - `UserCapability.granted_by = actor_user_id`。
  - 默认 `reason = admin_manual_grant`。
- `source == "invite_code"`：
  - `actor_user_id` **必须为 None**（否则 `ValueError`）。
  - `UserCapability.granted_by = None`。
  - 默认 `reason` **不得**为 `admin_manual_grant`（保持 `None`）。
- legacy 物化：管理员操作传 `actor_user_id`；邀请码续期物化时 `actor` 为空；`legacy_materialized` 记录的 `granted_by` 按真实来源保存。

非法组合（`admin_grant` + actor=None；`invite_code` + actor 非空）一律抛 `ValueError`。

### PV2-B09 审计 request_id 与 legacy 物化

管理员 grant/revoke/quota change 写审计时传真实 `request_id`（复用请求链的 `x-request-id`；请求链未提供则为 `None`，禁止伪造随机值）。

`_lock_and_materialize_legacy` 返回本次实际物化的 Capability 快照列表；`CapabilityMutationResult` 携带 `materialized_capabilities`。管理员首次操作触发物化时，审计 `after_data` 必须包含 `materialized_capabilities`（未物化为空列表），保证 legacy 物化来源可追踪。
