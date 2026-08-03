# CHANGE-20260803-001 权限与邀请码模型 V2 统一重构

| 项 | 值 |
|---|---|
| 日期 | 2026-08-03 |
| 类型 | behavior + contract + architecture |
| 影响范围 | 权限解析服务 / 认证路由 / 管理后台 / 邀请码合同 / 前端登录 |
| 业务代码 | backend/app（权限服务、认证、admin、schema）+ frontend（登录、会员页） |
| 数据操作 | **零操作**（未连接生产、未执行 backfill apply、未改 canary） |
| 前置提交 | `ea21f1b`（权限 V2 基线） |

## 1. 为什么改

测试账号进 /forbidden 的非数据根因：后端 login/`/me/access` **不返回 capabilities**（schema 有字段但端点漏填），且前端登录时把 capabilities 硬编码 `{}` 丢弃，导致路由守卫读到空权限 → 判 /forbidden。旧模型存在 Subscription/Plan 与 UserCapability 双权限真源。

## 2. 改了什么

### 2.1 权限唯一真源

- 新增 `resolve_effective_access`（effective_access_service）：user_capabilities 是唯一功能判权真源，Subscription/Plan 不参与判权，legacy fallback 显式 `source=legacy_plan_fallback`。
- `get_access_context` 改为兼容包装，内部统一调用 resolve，不再重复查询推导。
- 修复生产风险：ORM select 替代原始 SQL、UTC-aware 时区统一、复用 `_get_user_roles`、公开 `compute_default_route`/`infer_capabilities_from_plan`（不再导入私有）。

### 2.2 认证路由

- login/`/me/access` 返回 capabilities（C/D 断点）。
- 登录默认路由基于 capabilities（E 断点）：admin/仅self_selection→watchlist/仅research_replay→review/market族→market/无→forbidden。
- 前端登录消费 login 返回的 capabilities，不再硬编码空对象。

### 2.3 管理后台

- `GET /v1/admin/users` 与 `/admin/members` 返回权限摘要（capabilities/active_keys/has_any_access/default_route/capability_source/nearest_expires/legacy_fallback/subscription_summary）。
- 前端会员列表权限摘要列；抽屉默认权限概览，Tab 顺序：权限概览/账户信息/授权记录/审计。
- 修正过时文案："会员状态控制有效期，自选股额度由套餐决定" → "账户状态控制登录；功能范围和额度由当前有效权限决定"。

### 2.4 邀请码合同

- `InviteCodeCreate` 拒绝 capabilities=null/[]（旧 plan_code 模式生成被禁）。
- 邀请码预览显示权限组合 + watchlist_limit + 有效期 + 注册后默认入口。

### 2.5 迁移工具

- backfill dry-run 工具扩展统计（legacy fallback/部分权限/到期冲突/未知 plan_code/过期异常）与逐用户计划输出；只读、幂等、canary、apply 未实现。

## 3. 受影响契约

| 契约 | 变化 |
|---|---|
| 功能判权 | plan_code 不再决定功能权限，user_capabilities 唯一真源 |
| /me/access、login | 新增 capabilities 返回 |
| 登录默认入口 | 由 capabilities 计算 |
| 新邀请码 | 必须显式非空 capabilities |
| 管理员会员列表/抽屉 | 返回并展示权限摘要，默认权限概览 |

## 4. 验证

| 项 | 方式 | 结果 |
|---|---|---|
| 权限服务单元测试 | `test_effective_access_service.py` | 18 passed（含 aware/naive 时区） |
| 邀请码 schema | `test_gate2_capability_schemas.py` | 35 passed（含拒绝空/旧模式） |
| 前端 | tsc / eslint | 0 errors |
| PG 集成 | `test_permission_v2_pg_integration.py` | 新增（postgres marker，CI 临时库运行） |
| 检查器 | architecture / docs consistency / ruff / mypy | 通过 |

## 5. 状态

- 代码合同：`verified_local`（本地单元测试 + 前端 tsc/eslint）。
- PostgreSQL 集成：新增测试待 CI 临时容器运行。
- 真实部署：`deferred_with_reason`（待用户授权）。
- backfill apply：未执行。
