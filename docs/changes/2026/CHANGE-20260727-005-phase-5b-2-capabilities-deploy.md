# CHANGE-20260727-005：Phase 5B-2 Capability 模型 + 部署脚本修复

状态：已完成（Phase 5B-2：PA-01 capability 模型落地，部署脚本修复与静态测试；未启用自动部署）
日期：2026-07-27
类型：behavior / runtime
对应 PRD：`docs/prd/60-permissions-admin.md`、`docs/prd/80-system-runtime.md`
对应 Map：`docs/maps/60-permissions-admin.md`、`docs/maps/80-system-runtime.md`、`docs/maps/30-after-close.md`

## 1. 变更摘要

Phase 5B-2 完成两类重要变更：

1. **PRD60 PA-01 三类独立 capability 授权**：落地 `UserCapability` 模型与 `user_capabilities` 表，实现 per-capability 独立授予/撤销/过期；新增 `require_capability` 后端依赖（admin 豁免）；邀请码新增 `capabilities` JSONB 字段（PA-20）；前端引入 `CapabilityRoute` 替代旧 `SubscriberRoute`。
2. **部署脚本修复**：修复 `panji-deploy.sh` 的 stale 引用、错误容器名、detached HEAD、dry-run 措辞、state 目录初始化等问题，新增 `panji-deploy.test.sh` 静态测试。

## 2. 背景与问题

- **PRD60 PA-01 偏差**：Phase 5B-1 审计发现三类权限（自选/行情/复盘）绑定为 `plan_code` 套餐，无法单独授予/撤销；邀请码只能选定 `plan_code`，不能组合 capability；无 per-capability expires_at。详见 `maps/60-permissions-admin.md` §7 已知偏差。
- **部署脚本偏差**：`panji-deploy.sh` 存在 stale 引用（未先 `git fetch origin main`）、错误容器名（`trading-worker-calendar-scheduler`，实际为 `trading-worker-calendar`）、部署后留在 detached HEAD、dry-run 误称"健康检查"、state 目录未初始化等问题。

## 3. 变化内容

### 3.1 Capability 模型（PA-01~PA-20）

- **三类独立 capability**：`self_selection` / `market_data` / `research_replay`（`backend/app/models/user_capability.py`）
- **`UserCapability` 模型**：`user_capabilities` 表，per-capability 独立 `expires_at`、`watchlist_limit`（仅 self_selection）、`source`（invite_code/admin_grant/migration）
- **Migration**：`068_user_capabilities.py`（建表 + 从现有有效订阅回填）、`069_invite_code_capabilities.py`（邀请码 capabilities JSONB）
- **`require_capability(capability)`**：`access_control_service.py:413`，admin 豁免，替代 `require_feature`
- **API 调用点**：`market.py`、`stock_context.py`、`watchlist.py`
- **邀请码 `capabilities` JSONB**：`invitation.py:91`（PA-20），兑换时为每个 capability 创建独立行
- **前端 `CapabilityRoute`**：`routeStructure.ts` 新增 `'capability'` guard 替代 `'subscriber'`
- **路由守卫**：`/market`→self_selection、`/stock/:symbol`→market_data、`/replay`→research_replay
- **ForbiddenPage（403）**：`/forbidden`，已登录但缺少 capability 时渲染
- **Fallback 推断**：`user_capabilities` 无记录时按 `plan_code` 推断（observe_20→self_selection+market_data；research_50→全部三类）

### 3.2 部署脚本修复（`scripts/deploy/panji-deploy.sh`）

| 修复项 | 说明 |
|---|---|
| `git fetch origin main` | SHA 校验前先 fetch，避免本地 origin 引用过期 |
| calendar 容器名 | 改为 `trading-worker-calendar`（非 `-scheduler`） |
| 部署后 `git checkout main` | 避免 detached HEAD |
| dry-run 措辞 | 使用"计划验证"而非"健康检查" |
| state 目录初始化 | `STATE_FILE` 父目录不存在时 `mkdir -p` |
| 新增 `panji-deploy.test.sh` | 16 项静态断言测试 |

## 4. 影响范围

### 数据

- 新增 `user_capabilities` 表（Migration 068/069）
- 从现有有效 `Subscription` 回填 `user_capabilities` 行（observe_20/research_50 → capability 行）
- 旧 `Subscription` / `plan_code` / `entitlement_snapshot` 保留兼容期

### API 或契约

- 新增 `require_capability(capability)` 依赖，admin 豁免
- `require_feature` 保留兼容期

### 前端

- `CapabilityRoute` 替代 `SubscriberRoute`（guard 类型 `'subscriber'` → `'capability'`）
- 新增 `/forbidden` 403 页面

### 部署与运行

- `panji-deploy.sh` 行为变化：fetch 前置、容器名修正、部署后切回 main、dry-run 措辞、state 目录初始化
- 部署工作流（`deploy-production.yml`）不变：`workflow_run` on CI success + `workflow_dispatch`，SSH 到 `panji-prod`
- 自动部署链路启用状态不变（未启用）

## 5. 迁移与兼容

- Migration 068 创建 `user_capabilities` 表并从现有有效订阅回填，不破坏现有 `Subscription`
- 旧 `Subscription` / `plan_code` / `entitlement_snapshot` 保留兼容期，新读取优先、旧数据 fallback
- `require_feature` 保留兼容期，未立即移除
- 邀请码 `capabilities` JSONB 为可选字段，旧邀请码无该字段时按 `plan_code` 推断

## 6. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| UserCapability 模型 | 表结构、唯一约束、字段 | PASS | `user_capability.py` 自测入口；代码级核验 |
| require_capability | admin 豁免、capability 检查 | PASS（代码级） | `access_control_service.py:413` |
| 邀请码 capabilities JSONB | 字段存在、none_as_null | PASS（代码级） | `invitation.py:91` |
| CapabilityRoute | guard 类型、路由守卫映射 | PASS（代码级） | `routeStructure.ts`；契约测试 |
| Fallback 推断 | observe_20/research_50 映射 | PASS（代码级） | `068_user_capabilities.py` 回填逻辑 |
| 角色组合测试矩阵 | admin/observe_20/research_50/无订阅 × capability 组合 | 部分实现 | 测试矩阵未完整覆盖 |
| 部署脚本修复 | 5 项修复点 | PASS（静态） | `panji-deploy.test.sh` 16 项断言 |
| 部署脚本运行时 | 真实部署 | 未验证 | 链路未启用，未触发真实部署 |

## 7. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | 无变化（PRD60 PA-01 已定义目标行为） |
| `maps/60-permissions-admin.md` | 新增 §11 Phase 5B-2 PA-01 capability 授权；更新核验状态 |
| `maps/80-system-runtime.md` | 新增 §12 Phase 5B-2 部署脚本修复；更新核验状态 |
| `maps/30-after-close.md` | 新增 §10 Phase 5B-2 影响说明（after-close 链路不变） |
| Runbooks | 无变化 |

## 8. 回滚方案

- **Capability 模型**：Migration 068/069 可回滚（downgrade 删除 `user_capabilities` 表与 `capabilities` 列）；回滚后恢复 `require_feature` 路径；旧 `Subscription` / `plan_code` 数据未删除，兼容期可继续使用
- **部署脚本**：`panji-deploy.sh` 修改可 git revert；回滚后恢复旧容器名与 detached HEAD 行为（不推荐）
- **数据**：`user_capabilities` 表为新增，回滚不丢失原 `Subscription` 数据；回填行可删除

## 9. 遗留问题与风险

- 角色组合测试矩阵（`maps/60` §10.7）部分实现，未完整覆盖所有 capability 组合与过期场景
- 部署脚本仅静态测试，未触发真实部署（链路未启用）
- `require_feature` 保留兼容期，清理时点未定
- `monitor_limit` → `watchlist_limit` 命名统一未完成（§10.3/§10.8 第 6 步）

## 10. 后续变化

- 完成 §10.7 测试矩阵
- 启用自动部署链路（需服务器侧配置 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件、GitHub Secrets）
- 清理 `require_feature` 与 `entitlement_snapshot.features` 兼容路径
