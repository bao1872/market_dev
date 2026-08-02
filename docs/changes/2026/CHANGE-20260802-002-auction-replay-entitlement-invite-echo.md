# CHANGE-20260802-002 — 竞价归入复盘权限 + 邀请码权限回显

- 日期：2026-08-02
- 类型：behavior / contract（权限边界 + 数据回显）
- 领域：权限（PRD60 PA-12 / PA-20 / PA-21）、前端导航与邀请码管理后台
- 关联 PRD：`docs/prd/60-permissions-admin.md`（PA-12 复盘与竞价、PA-20/PA-21 邀请码）
- 关联 Maps：`docs/maps/60-permissions-admin.md` §11
- 状态：`verified_code` / `runtime_pending`
  - `verified_code`：代码 + 纯单元测试（后端 32 项、前端 35 项）+ TSC + ESLint 本地复验通过（2026-08-02 复核）。
  - `runtime_pending`：生产链路真实运行未核验，需在 Live Mount 部署后按 §5 逐项验收。
  - CI 已收敛为手动 `workflow_dispatch` 诊断，不构成本 Change 的验收前置或阻断条件。

## 1. 背景与意图

原竞价分析模块（`/auction`、`/auction/board/:boardId`、`/auction/stock/:symbol`）的 5 个 GET 端点使用 `require_authenticated`，即任何已登录用户均可读取，与复盘模块（`research_replay`）的权限边界不一致。同时，邀请码管理后台创建/查询/撤销接口未回显 `capabilities` 字段，管理员无法确认邀请码实际授予的能力组合，审计日志也缺少 capability 维度。

本轮目标（范围严格限定，不做 bootstrap / Review 数据 / GHCR / Release Gate / 生产部署）：

1. 竞价读取归入 `research_replay` 单一 capability（中文统一展示"复盘与竞价"），与复盘共用同一机器值与后端守卫契约。
2. 邀请码 `capabilities` 在创建响应、列表响应、创建/撤销审计中正确回显。
3. 全站中文标签 "复盘研究"/"复盘管理" 统一为 "复盘与竞价"，机器值 `research_replay` 不变。

## 2. 修改前后关键差异

### 2.1 后端：竞价权限（verified_code）

| 项 | 修改前 | 修改后 |
|---|---|---|
| 5 个 GET 端点依赖 | `require_authenticated` | `require_capability("research_replay")`（admin 豁免） |
| 模块文档权限注释 | "登录即可" 类表述 | `[PRD60 PA-01] 竞价读取权限 = 复盘权限` |
| 2 个 admin POST（`trigger_scan` / `trigger_anchors`） | `require_admin` | 不变（仍是 `require_admin`） |
| 常量 | 无 | `AUCTION_CAPABILITY = "research_replay"`（与 `review.py` 同值） |

调用点：`backend/app/api/auction.py:88` 常量定义；5 处 GET 端点 `Depends(require_capability(AUCTION_CAPABILITY))`。
无业务逻辑改动，不新增 auction capability。

### 2.2 后端：邀请码 capabilities 回显（verified_code）

文件 `backend/app/api/admin_subscription.py`：

- 创建响应 `InviteCodeResponse` 增加 `capabilities=invite.capabilities`；创建审计 `after_data` 增加 `"capabilities": invite.capabilities`。
- 列表响应 `InviteCodeListItem` 增加 `capabilities=invite.capabilities`。
- 撤销：审计 `before_data={"status": "unused", "capabilities": invite.capabilities}`，`after_data` 含 capabilities；响应增加 `capabilities=invite.capabilities`。

不新增 migration；`InviteCode.capabilities` 字段（JSONB，migration 069）已存在。`subscription_service.generate_invite_codes` 已按显式 `capabilities` 存储，不被 `plan_code` 覆盖（plan_code 仅定 monitor_limit）；fallback 推断 `observe_20 → self_selection+market_data`、`research_50 → +research_replay` 保持不变。

### 2.3 前端：路由与导航（verified_code）

- `frontend/src/App.tsx`：3 条竞价路由合并到 `CapabilityRoute capability={REPLAY_AND_AUCTION_CAPABILITY}`（="research_replay"），与 `/review` 同守卫；直接 URL 访问无权限时走现有 `/forbidden` 流程。
- `frontend/src/navigation/capabilities.ts`（新增）：能力标签单一真源，`REPLAY_AND_AUCTION_CAPABILITY='research_replay'`、`CAPABILITY_LABELS.research_replay='复盘与竞价'`、`formatCapabilityGrants`、`hasCapability` 等。
- `frontend/src/navigation/appNavigation.ts`：`AppNavItem` 增加 `requiredCapability?`；`USER_NAV_ITEMS` 中自选 = `self_selection`、复盘/竞价 = `research_replay`；新增纯函数 `filterNavItemsByCapability(items, capabilities, isAdmin)`。
- `frontend/src/layouts/UserAppShell.tsx`：导航过滤改用 `filterNavItemsByCapability`（无 research_replay → 同时隐藏复盘与竞价；无 self_selection → 隐藏自选；admin 豁免）。
- `frontend/src/navigation/routeStructure.ts`：三条竞价路由纳入既有 capability 守卫节点（与 `/review` 同组）。
- `frontend/src/pages/AdminUsersPage.tsx`：能力状态/勾选/预览/校验/撤销确认全部使用 `CAPABILITY_LABELS`；生成码与列表 `capabilities` 列使用 `formatCapabilityGrants`，空 capability 显示"按套餐授权（未返回 capability 组合）"。
- `frontend/src/api/endpoints.ts`：`InviteCode.capabilities` / `InviteCodeListItem.capabilities` 由可选 `?` 改为 `capabilities: CapabilityGrantInput[] | null`（null = 旧模式，非后端遗漏）。
- `frontend/src/styles/global.scss`：补充 `.generated-invite-caps` / `.cap-status-head .cap-status-key` 样式（修正变量 `$color-text-muted` → `$color-muted`）。

### 2.4 文档（verified_code）

- `docs/prd/60-permissions-admin.md`：PA-01 三类权限标签、PA-12 由"复盘管理"扩展为"复盘与竞价"（含竞价模块），机器值 `research_replay` 不变。
- `docs/maps/60-permissions-admin.md` §11.1 / §11.3 / §11.5：更新 `research_replay` 语义映射、auction.py 调用点、竞价三条路由守卫映射。
- `docs/changes/INDEX.md`：新增本条目。

## 3. 受影响契约

- 后端 403 契约：无 `research_replay` 用户访问竞价 GET 端点返回 403，与复盘一致性。
- 前端导航语义：无 `research_replay` 时复盘与竞价入口一同隐藏；admin 始终可见。
- 邀请码回显：创建/列表/撤销响应与审计含 capability 维度，便于权限核对。
- 不引入独立 auction capability，不新增 migration，不修改 main / 不部署生产。

## 4. 测试（verified_code，2026-08-02 本地复验）

- 后端 `backend/tests/test_auction_replay_entitlement.py`：`PURE_UNIT_TEST=1` 下 32 项通过（全 mock 无 DB）。覆盖：无 `research_replay` 访问竞价 GET → 403；有 `research_replay` → 200；未登录 → 401；admin POST 端点仍走 `require_admin` 未被放宽；邀请码创建/列表/撤销响应含 `capabilities`；plan fallback；显式 capabilities 优先；不存在独立 auction capability。
- 前端 `frontend/src/navigation/__tests__/replayAuctionEntitlement.test.ts` 与 `routeStructure.test.ts`：`node --experimental-strip-types --test` 合计 35 项通过。覆盖：无 `research_replay` 时复盘与竞价入口一同隐藏；有该 capability 时一同显示；admin 始终可见。
- `frontend/node_modules/.bin/tsc --noEmit`：0 错误。
- `frontend/node_modules/.bin/eslint src/navigation src/pages/AdminUsersPage.tsx`：0 错误（4 条既有 `react-hooks/exhaustive-deps` 警告，与本次权限修改无关，未新增）。

## 5. 验证与未完成项

- `verified_code`：代码修改、纯单元测试、TSC、ESLint、文档同步已完成并于 2026-08-02 复验。
- `runtime_pending`：生产链路真实运行未核验。需在 Live Mount 部署后按下列三条证据验收：
  1. 无 `research_replay` 账户请求竞价 GET 端点实际返回 403；
  2. 有 `research_replay` 账户导航同时出现"复盘与竞价"两个入口；
  3. 邀请码创建/列表接口响应体实际包含 `capabilities` 字段。
- CI 定位说明：CI 已收敛为手动 `workflow_dispatch` 诊断，非部署门禁，不作为本 Change 的验收前置条件（原"待 Fast CI"表述已废止）。
- 明确不做：生产部署、bootstrap、Review 数据闭环、main 分支修改、Migration 生产应用、pointer 发布。
