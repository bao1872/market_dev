# CHANGE-20260803-002 权限模型 V2 后端合同收口

| 项 | 值 |
|---|---|
| 日期 | 2026-08-03 |
| 类型 | behavior + contract |
| 影响范围 | 授权服务 / 撤销服务 / 管理员 API / Schema / 纯单元测试 |
| 业务代码 | backend/app（subscription_service、effective_access_service、admin_subscription、schemas/access、schemas/subscription） |
| 数据操作 | **零操作**（不连接共享数据库、不新增 migration、不 backfill） |
| 前置提交 | `212d037`（阶段A 收口已推送 dev；本轮在保留该三个提交基础上二次纠偏，不改写历史） |

## 1. 为什么改

权限模型 V2 已确认：user_capabilities 是功能判权唯一真源，Subscription 仅商业记录。但统一授权生命周期尚未收口：`apply_capability_grant` 初稿无条件物化 legacy 权限（会导致显式邀请码新注册获得超出邀请码配置的套餐隐含权限）、无用户行锁（并发物化会唯一约束冲突）、参数混用 months、撤销会覆盖原 granted_by、admin_revoke 未来到期记录仍可能解析为 active、管理员 access-profile 返回裸 dict 无 Schema、商业状态无 fail-closed 解析、Grant/Revoke 无 reason。

## 2. 改了什么

### 2.1 统一授权与锁合同（PV2-B01/B02/B03）

- `apply_capability_grant` 只接收确定性 `grant_days`，`source` 收窄为 `admin_grant`/`invite_code`，返回 `CapabilityMutationResult`（action/mutation_type/before/after/materialized_capabilities）。
- `months` 在调用边界转换为 `grant_days = months * 30`。
- 场景化 legacy 物化：显式邀请码新注册 `materialize_legacy=False`；旧用户续期/管理员首次管理 `materialize_legacy=True`。
- 需物化路径先 `SELECT User FOR UPDATE`（`_lock_and_materialize_legacy`）再物化，固定锁顺序避免反向锁依赖；`_lock_and_materialize_legacy` 返回本次实际物化快照列表（未物化为空）。
- 期限算法：active 顺延；expired/revoked/空 从注入 now 重算；tombstone 重新授权恢复真实来源并更新授予者。

### 2.2 source/actor 合同（PV2-B08）

- `apply_capability_grant` 公共签名只保留 `actor_user_id`，消除 `granted_by`/`actor_user_id` 语义重叠。
- `admin_grant`：`actor_user_id` 必填（`granted_by=actor_user_id`，默认 reason=admin_manual_grant）。
- `invite_code`：`actor_user_id` 必须为 None（`granted_by=None`，默认 reason 不写 admin_manual_grant）。
- 非法组合（admin_grant+actor=None；invite_code+actor 非空）抛 `ValueError`。

### 2.3 撤销 tombstone、quota change 与审计（PV2-B04/B05）

- `revoke_capability_from_user` 返回 `CapabilityMutationResult`；撤销保留原 `granted_by`，新 tombstone `granted_by=None`，重复撤销幂等。
- `resolve_effective_access` 对 `admin_revoke` 强制 `active=False`。
- 新增独立服务 `change_self_selection_quota`（mutation_type=quota_change，不修改 expires_at，revoked 不可恢复）+ API `PATCH /users/{user_id}/capabilities/self_selection/quota`。
- 管理员 grant/revoke/quota API 同事务写结构化审计：`target_type="user_capability"`，`target_id="{user_id}:{capability}"`；`action` 依据真实 `mutation_type` 生成（`capability.grant/extend/extend_and_quota_change/regrant/quota_change/revoke`），不得全部记成同一 action。
- `CapabilityMutationResult` 含 `mutation_type`（grant/extend/extend_and_quota_change/regrant/quota_change/revoke）；grant/revoke API 的 after 快照含 `mutation_type` 和 `materialized_capabilities`（首次物化非空，未物化空列表）。
- grant after 含 granted_by/actor/reason（默认 `admin_manual_grant`），revoke after 含 revoked_by/actor/reason（默认 `admin_manual_revoke`）。
- `DELETE /users/{user_id}/capabilities/{capability}` 接收可选 `reason` query（去空白/空转 None/限长 500），传入 `revoke_capability_from_user`。
- grant/revoke/quota 端点从 `request.headers.get("x-request-id")` 取 `request_id` 传给 `write_audit_log`（不伪造随机值）。

### 2.4 商业状态三入口 + 真正 Schema 化（PV2-B06/B07）

- 唯一纯商业状态解析器 `resolve_commercial_status`，**三个入口共用**：`get_effective_subscription_status`（六态）、`list_subscribers`、`get_user_access_profile`。禁止入口自行比较 expires_at。
- `schemas/access.py` 管理员 access-profile 分层模型改为真实类型：`AdminAccountInfo`（id: UUID，created_at/last_login_at: datetime|None）、`EffectiveAccessInfo`（default_route 必填）、`SubscriptionSummaryInfo`（status: Literal 六态，starts_at/expires_at: datetime|None）、`ExplicitCapabilityRecord`（capability/state: Literal，granted_at/expires_at: datetime|None，granted_by: UUID|None）。
- 端点直接传 datetime/UUID，由 Pydantic 序列化为 ISO，禁止手工 isoformat。
- `schemas/subscription.py` 新增 `ChangeSelfSelectionQuotaRequest`。

## 3. 受影响契约

| 契约 | 变化 |
|---|---|
| 统一授权 | `apply_capability_grant` 改 `grant_days` + `materialize_legacy` + 只保留 `actor_user_id` + 返回 mutation result（含 materialized_capabilities） |
| source/actor | admin_grant 必填 actor / invite_code 必无 actor；granted_by 按来源派生；默认 reason 区分 |
| 新注册物化 | 显式邀请码新注册不再物化套餐推导权限 |
| 并发 | 需物化路径统一先锁 User 行 |
| mutation_type | apply 只产生 grant/extend/extend_and_quota_change/regrant；纯 quota_change 由独立 change_self_selection_quota 产生 |
| 撤销 | tombstone 保留原 granted_by；admin_revoke 强制 inactive；幂等 |
| 审计 | 结构化 target_id/before/after/actor/reason；action 依据真实 mutation_type 生成（grant/extend/extend_and_quota_change/regrant/quota_change/revoke）；首次物化含 materialized_capabilities；传真实 request_id |
| 撤销 reason | DELETE 撤销路由新增可选 `reason` query（去空白/空转 None/限长 500） |
| access-profile | 绑定正式 Pydantic 分层 Schema（真实 datetime/UUID/Literal）；商业状态 fail-closed 解析 |
| 商业状态 | 三入口共用 resolve_commercial_status，禁止自行比较 expires_at |
| Grant/Revoke 请求 | 新增可选 reason；新增 ChangeSelfSelectionQuotaRequest |
| AccessContext | default_route 必填（测试夹具已补） |

## 4. 验证

| 项 | 方式 | 结果 |
|---|---|---|
| 权限合同测试 | `test_permission_v2_backend_contracts.py`（PURE_UNIT_TEST=1，mock 不连库） | **49 passed**（锁合同/source-actor/mutation_type/独立 quota change/商业三入口复用/access-profile Schema 受限类型/审计 request_id+materialized） |
| 权限定向测试集 | contracts + lifecycle + gate2 | 62 passed / 7 skipped（PG 集成纯单元模式 skip） |
| 权限扩展定向集 | 上述 + effective_access + access_control_service | **105 passed / 22 skipped** |
| shared DB 目标测试代码 | `test_permission_v2_pg_integration.py` 扩展 14 项真实 PG 合同 | **已写代码，未运行**（PURE_UNIT_TEST=1 不执行 shared_dev_db） |
| Ruff | 修改文件 | All checks passed |
| Mypy | 修改生产文件 | Success: no issues found |
| 文档一致性 | `tools/check_docs_consistency.py` | PASS |
| 架构 | `tools/check_architecture.py` | 0 violations |
| 治理 | `tools/check_governance_rules.py` | PASS |

## 5. 状态

- 代码合同：`verified_local`（本地纯单元 49 + 定向 62 / 7 skip + 扩展 105 / 22 skip，Ruff + Mypy + 检查器通过）。
- 本轮纠偏（2026-08-03 二次收口）：source/actor 收窄、mutation_type 语义修正（apply 不再产生纯 quota_change）、独立 quota change 入口、商业状态三入口统一、access-profile 真正 Schema 化（datetime/UUID/Literal）、审计 request_id + materialized_capabilities。未修改前端、无 Migration、无部署文件、无 canary 改动。
- PostgreSQL 集成（`test_permission_v2_pg_integration.py`）：**本轮已扩展 14 项真实 PG 合同测试代码，但未运行**（纯单元模式 skip，待授权共享开发库目标验证）。
- 待提交并推送 dev（本轮收口，保留原有三个提交，不改写历史）。
- 真实部署：`deferred_with_reason`（用户明确本轮不部署）。
- 共享开发库目标验证：`pending_authorization`（待用户授权）。
- backfill：未执行。
