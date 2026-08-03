# CHANGE-20260803-002 权限模型 V2 后端合同收口

| 项 | 值 |
|---|---|
| 日期 | 2026-08-03 |
| 类型 | behavior + contract |
| 影响范围 | 授权服务 / 撤销服务 / 管理员 API / Schema / 纯单元测试 |
| 业务代码 | backend/app（subscription_service、effective_access_service、admin_subscription、schemas/access、schemas/subscription） |
| 数据操作 | **零操作**（不连接共享数据库、不新增 migration、不 backfill） |
| 前置提交 | `2e3db61`（权限 V2 待收口基线，工作树含 apply_capability_grant 初稿） |

## 1. 为什么改

权限模型 V2 已确认：user_capabilities 是功能判权唯一真源，Subscription 仅商业记录。但统一授权生命周期尚未收口：`apply_capability_grant` 初稿无条件物化 legacy 权限（会导致显式邀请码新注册获得超出邀请码配置的套餐隐含权限）、无用户行锁（并发物化会唯一约束冲突）、参数混用 months、撤销会覆盖原 granted_by、admin_revoke 未来到期记录仍可能解析为 active、管理员 access-profile 返回裸 dict 无 Schema、商业状态无 fail-closed 解析、Grant/Revoke 无 reason。

## 2. 改了什么

### 2.1 统一授权与锁合同（PV2-B01/B02/B03）

- `apply_capability_grant` 只接收确定性 `grant_days`，`source` 收窄为 `admin_grant`/`invite_code`，返回 `CapabilityMutationResult`（action/before/after）。
- `months` 在调用边界转换为 `grant_days = months * 30`。
- 场景化 legacy 物化：显式邀请码新注册 `materialize_legacy=False`；旧用户续期/管理员首次管理 `materialize_legacy=True`。
- 需物化路径先 `SELECT User FOR UPDATE`（`_lock_and_materialize_legacy`）再物化，固定锁顺序避免反向锁依赖。
- 期限算法：active 顺延；expired/revoked/空 从注入 now 重算；tombstone 重新授权恢复真实来源并更新授予者。

### 2.2 撤销 tombstone 与审计（PV2-B04/B05）

- `revoke_capability_from_user` 返回 `CapabilityMutationResult`；撤销保留原 `granted_by`，新 tombstone `granted_by=None`，重复撤销幂等。
- `resolve_effective_access` 对 `admin_revoke` 强制 `active=False`。
- 管理员 grant/revoke API 同事务写结构化审计：`target_type="user_capability"`，`target_id="{user_id}:{capability}"`；`action` 依据真实 `mutation_type` 生成（`capability.grant/extend/quota_change/extend_and_quota_change/regrant/revoke`），不得全部记成同一 action。
- `CapabilityMutationResult` 新增 `mutation_type` 字段精确区分五态 + 撤销；grant after 含 granted_by/actor/reason（默认 `admin_manual_grant`），revoke after 含 revoked_by/actor/reason（默认 `admin_manual_revoke`）。
- `DELETE /users/{user_id}/capabilities/{capability}` 接收可选 `reason` query（去空白/空转 None/限长 500），传入 `revoke_capability_from_user`，消除 RevokeCapabilityRequest 死 Schema 风险（reason 语义由路由 + Schema 校验共同承载）。

### 2.3 商业状态与响应 Schema（PV2-B06/B07）

- 新增 `resolve_commercial_status` 纯商业状态解析器（none/pending/active/expired/revoked/cancelled + 诊断 reason），异常周期 fail-closed 判 expired。
- `schemas/access.py` 新增管理员 access-profile 分层模型（AdminAccessProfileResponse 等）；旧 AccessProfileResponse 自测字段数更新为 16。
- `schemas/subscription.py` Grant/Revoke 请求新增可选 `reason`（去空白、空转 None、限长 500）。
- `get_user_access_profile` 端点绑定 `AdminAccessProfileResponse`，商业状态用 `resolve_commercial_status`。

## 3. 受影响契约

| 契约 | 变化 |
|---|---|
| 统一授权 | `apply_capability_grant` 改 `grant_days` + `materialize_legacy` + 返回 mutation result |
| 新注册物化 | 显式邀请码新注册不再物化套餐推导权限 |
| 并发 | 需物化路径统一先锁 User 行 |
| 撤销 | tombstone 保留原 granted_by；admin_revoke 强制 inactive；幂等 |
| 审计 | 结构化 target_id/before/after/actor/reason；action 依据真实 mutation_type 生成（capability.grant/extend/quota_change/extend_and_quota_change/regrant/revoke） |
| mutation_type | `CapabilityMutationResult` 新增 `mutation_type`，精确区分授权五态 + 撤销 |
| 撤销 reason | DELETE 撤销路由新增可选 `reason` query（去空白/空转 None/限长 500） |
| access-profile | 绑定正式 Pydantic 分层 Schema；商业状态 fail-closed 解析 |
| Grant/Revoke 请求 | 新增可选 reason |
| AccessContext | default_route 必填（测试夹具已补） |

## 4. 验证

| 项 | 方式 | 结果 |
|---|---|---|
| 权限合同测试 | `test_permission_v2_backend_contracts.py`（PURE_UNIT_TEST=1，mock 不连库） | 28 passed（锁合同/物化矩阵/reason/商业状态/撤销保留 granted_by/mutation_type 六态/expires_at=None 安全/无记录 tombstone/幂等撤销） |
| 权限定向测试集 | contracts + lifecycle + gate2 | 62 passed / 7 skipped（PG 集成纯单元模式 skip） |
| 权限扩展定向集 | 上述 + effective_access + access_control_service | 84 passed / 22 skipped |
| Ruff | 修改文件 | All checks passed |
| Mypy | 修改生产文件 | Success: no issues found |
| 文档一致性 | `tools/check_docs_consistency.py` | PASS |
| 架构 | `tools/check_architecture.py` | 0 violations |
| 治理 | `tools/check_governance_rules.py` | PASS |

## 5. 状态

- 代码合同：`verified_local`（本地纯单元测试 28 + 定向 62 / 7 skip + 扩展 84 / 22 skip，Ruff + Mypy + 检查器通过）。
- 本轮终审修复：`CapabilityMutationResult` 新增 `mutation_type`；审计 action 依据真实 mutation_type 生成；DELETE 撤销路由新增可选 `reason` query（消除 RevokeCapabilityRequest 死 Schema 风险）。未修改前端。
- PostgreSQL 集成（`test_permission_v2_pg_integration.py`）：纯单元模式下 skip；真实共享开发库目标测试待后续授权轮次运行。
- 已提交并推送 dev（阶段A 收口）。
- 真实部署：`deferred_with_reason`（用户明确本轮不部署）。
- 共享开发库目标验证：`pending_authorization`（阶段B，待用户授权）。
- backfill：未执行。
