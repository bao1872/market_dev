> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 操作分支：`fix/release-remaining-alignment-gaps`  
> Phase D 起始代码基线（implementation_base_commit）：`64ed75cce80f5b3f2b5ab95f96b52aac11475e3e`  
> 已验证实现 Commit（verified_implementation_commit）：`4e146f0bcc1d3d05e51f4fe968913eac74651778`  
> 验证日期：2026-07-02  
> 验证状态：本地验证完成；CI 未验证；生产未验证  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件；文档 Commit 不记录自身 SHA，最终 release HEAD 在 PR 描述与合并后 CHANGE 中补记。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 18 代码、文档与生产反馈对齐表

本表是代码基线 `4e146f0bcc1d3d05e51f4fe968913eac74651778`（verified_implementation_commit）的实现事实快照，只记录“当前确认设计已经明确，但实现、测试、部署或生产表现尚未一致”的问题。未决产品问题进入 `17-open-decisions.md`；历史经过进入 CHANGE。

只要本表仍有未关闭条目，就只能声明“文档已准确对齐当前代码及其差异”，不能声明“代码已经完全符合设计”。

| ID | 领域 | 当前证据与差异 | 目标设计 | 状态 | 关闭证据 |
|---|---|---|---|---|---|
| ALIGN-001 | Git 分支 | 主要改动位于 `refactor/access-v2-platform-recovery`，`main` 仍是较早基线 | 从修复分支建立精确 release 候选，最终通过 PR merge commit 合入 main | `KNOWN_GAP` | release 候选尚未建立 |
| ALIGN-002 | 历史迁移 | feature 分支 diff 包含 022–027、040 等既有 migration 的变更，尚未完成逐文件语义审计 | 已执行历史 migration 不修改；新结构只新增 migration | `CLOSED` | 基线清理提交 `65a0f91` 已逐文件审计并恢复 7 个历史 migration 为原始已执行版本 |
| ALIGN-003 | 算法文件 | feature 分支包含大量 algorithm asset 改动，尚无完整的公式、参数和输出字段等价性证据 | 证明无语义变化，否则撤销或独立评审 | `CLOSED` | 基线清理提交 `65a0f91` 已完成 algorithm asset 公式/参数/窗口/输出字段/事件条件语义审计，确认与 main 一致 |
| ALIGN-004 | 趋势覆盖 | 已出现 total 5223、成功约 729、大量 100ms timeout，残缺批次可被发布 | computable universe 100% 结果覆盖；partial_failed 不发布 | `CLOSED` | `tests/test_strategy_batch.py` 7 passed；`backend/reports/dsa_benchmark_20260702.md` 350 只全部成功无失败无跳过；代码提交 `8c991e3` 实现严格发布门禁 |
| ALIGN-005 | DSA 预算 | 当前存在 100ms 单股硬中断 | 用代表性基准确定预算，超时计 failed，不得当筛选 | `CLOSED` | `backend/reports/dsa_benchmark_20260702.md` 给出冷/热启动 p50/p90/p95/p99/max；提交 `8c991e3` 取消 100ms 硬中断并改为 run 级总超时，单股超时记 failed |
| ALIGN-006 | Watchlist 权限 | 代码中曾出现新旧额度检查函数并存，读接口未全部要求有效订阅 | 全部 watchlist API 复用 AccessContext + active subscription | `CLOSED` | `tests/test_watchlist_permission_uses_access_context.py` + `tests/test_watchlist.py` + `tests/test_watchlist_limit.py` 共 30 passed；代码提交 `c06a2ea` |
| ALIGN-007 | 趋势 API 权限 | 部分结果接口仅检查 feature，过期用户仍可能保留 feature | active subscription + feature 双重检查 | `CLOSED` | `tests/test_trend_selection_api_permissions.py` 20 passed；代码提交 `c06a2ea` |
| ALIGN-008 | Worker 资格 | `eligible_user_service.py` 已存在，但尚无证据证明 Monitor、Recipient、Outbox、Delivery 全链路均复用并在投递前复核 | active member + active subscription，投递前复核 | `CLOSED` | `tests/test_eligible_user_service.py` + `tests/test_monitor_eligibility_integration.py` 共 19 passed；代码提交 `c06a2ea` |
| ALIGN-009 | 个股行情 | 图表场景 DB 有历史时不补尾部，且可能绕过实时 last-bar merge | 统一历史+尾部补齐+盘中 partial 聚合 | `CLOSED` | `tests/test_market_data_aggregation_service.py` + `tests/test_bars_api_db_first.py` + `tests/test_chart_bars_service.py` + `tests/test_indicator_service.py` 合并 49 passed；代码提交 `8c991e3` + 缓存修复 `c22940d` |
| ALIGN-010 | 飞书图片 | 后端已实现独立 card/image 状态、partial_failed、failed_step/error_code/error_message 三字段、仅重试图片、502 响应体解析；真实渠道生产 E2E 尚未执行 | 独立 card/image 状态、partial_failed、仅重试图片、真实 E2E | `KNOWN_GAP` | Phase C 已实现状态机（`stock_detail_feishu_service` 返回 partial_failed + failed_step/error_code/error_message，`capture_resp.raise_for_status()` 解析 502 响应体）；`tests/test_state_machine.py` 7 passed、`tests/test_stock_detail_feishu.py` 通过；真实渠道生产 E2E 待 Phase F 部署后执行 |
| ALIGN-011 | Capture Token | 前端已使用独立 storage key，但尚未完成普通 API 隔离、有效期和最小权限 E2E 证据 | 最小权限、短期、独立客户端、不污染登录 | `CLOSED` | `tests/test_capture_token_isolation.py` + `tests/test_auth_login.py` 共 18 passed；代码提交 `c06a2ea` |
| ALIGN-012 | 管理页面 | 当前后端已实现邀请码、会员列表、任务和部分审计；用户启停、直接授予/续期/撤销/改套餐 API 与对应页面尚不完整，部分控件曾仅 Toast | 无真实 API 的控件删除；所有成功来自服务器结果 | `KNOWN_GAP` | 后端 admin API 与测试已覆盖，admin 生产 E2E 尚未执行 |
| ALIGN-013 | 文档旧术语 | 仓库旧文档仍出现 plan_contract、Membership、旧到期路由等 | plans + Subscription + `/subscription-expired` | `CLOSED` | 全局扫描完成；当前设计文档已统一为 plans + Subscription + `/subscription-expired`；遗留 API 路径/字段已标注 V1.6 遗留命名；见 CHANGE-20260702-005 |
| ALIGN-014 | CI | `ruff`/`type-check` job 曾设置 `continue-on-error: true`，失败不阻断 workflow；`Ruff Changed Files` 相对 `main` 检查整个大型 feature diff 会失败 | release 候选最终 HEAD 的 blocking jobs 全绿，新增文件零错误，历史债务不新增/不增加 | `KNOWN_GAP` | 本地已验证三层 Ruff 配置：`Ruff New Files` 阻断新增 Python 文件，`Ruff Baseline Regression` 阻断历史债务新增/增加，`Ruff Full Repository Report` 非阻断上传报告；基线 commit `64ed75c`、诊断总数 930→903、无新增/增加诊断；但 GitHub Actions 尚未针对最终 HEAD 全绿，故保持 `KNOWN_GAP`；见 CHANGE-20260702-010 |
| ALIGN-015 | 运行服务 | CORE_ONLY 不包含 capture/outbox/delivery，可能造成文字有、图片无 | 部署能力与业务功能匹配；服务健康不可用时不假成功 | `KNOWN_GAP` | Phase 9 生产部署验证尚未执行 |
| ALIGN-016 | Node Cluster 输入 | `indicator_contract.py` 中 `NODE_CLUSTER_LOW_BARS=3600`，应为 250*16=4000 | 15m 输入 4000 根，1d=250，1m=2 | `CLOSED` | `NODE_CLUSTER_LOW_BARS` 已改为 `DAILY_HISTORY_BARS * NODE_CLUSTER_15M_BARS_PER_DAY = 4000`，新增 `NODE_CLUSTER_15M_BARS_PER_DAY=16`，`test_node_cluster_contract.py` 8 passed |
| ALIGN-017 | 飞书渠道 | Phase C 已永久删除 `FeishuWebhookAdapter`，统一为 `feishu_platform_app`；migration 055 添加 CHECK 约束禁止 `feishu_webhook` | Platform App only，删除全部 Webhook | `CLOSED` | `backend/app/services/feishu_webhook_adapter.py` 已删除；`notification_service.py` / `outbox_relay.py` / `beta_application_notifier.py` / `channel_adapter.py` / `system_channel.py` 改用 `FeishuPlatformAppAdapter`；migration 055 添加 CHECK 约束；`tests/test_feishu_platform_app_only.py` 11 passed；CHANGE-20260702-009 |
| ALIGN-018 | Capture 链路 | Phase C 已实现专用 `/capture/stock/:symbol` 前端路由 + `/api/v1/capture/stocks/{instrument_id}/snapshot` 后端 API + Capture Token 隔离 | 专用 `/capture/stock/:symbol` 路由 + Capture Token 校验 | `CLOSED` | `backend/app/api/capture.py` 新增 snapshot 端点（依赖 `get_capture_token_payload`，校验 type=capture + scope=stock_detail_capture + path/token instrument_id 一致）；`stock_capture_service.py` URL 改为 `/capture/stock/{symbol}?...&token=...`；前端 `App.tsx` 新增 `/capture/stock/:symbol` 路由（不经过 ProtectedLayout）+ `CaptureStockPage.tsx`；`create_capture_token` 增强 scope/instrument_id/user_id；`tests/test_capture_token_isolation.py` 9 passed + `tests/test_capture_snapshot.py` 6 passed；CHANGE-20260702-009 |
| ALIGN-019 | DSA 发布门禁 | `publish_run` 仍允许 partial_failed 状态发布 | partial_failed 禁止发布，仅 completed 可进入 published | `CLOSED` | `backend/app/services/strategy_batch_service.py` 的 `publish_run` 已将状态检查改为仅允许 `completed`，`partial_failed` 即使 `succeeded_count > 0` 也显式拒绝；`tests/test_dsa_publish_validation.py::test_publish_run_rejects_partial_failed_with_succeeded` 通过；`StrategyBatchService._check_quality_gates` 与 `publish_run` 门禁一致 |
| ALIGN-020 | 数量语义耦合 | `INDICATOR_BARS["15m"]=3600` 同时代表页面显示、API 返回和 Node 输入 | 拆分为 CHART_DISPLAY_BARS、INDICATOR_RESPONSE_BARS、NODE_CLUSTER_INPUT_SPEC | `CLOSED` | `INDICATOR_BARS["15m"]` 已改为引用 `NODE_CLUSTER_LOW_BARS`（不再硬编码 3600），`CHART_BARS_COUNT=250` 与 `NODE_CLUSTER_LOW_BARS=4000` 已分离，`test_node_cluster_contract.py` 验证通过 |
| ALIGN-021 | Ruff 历史债务 | 全仓库 `ruff check .` 当前存在 903 个历史错误（基线 930），分布于 tests、tools 和部分旧代码 | 全仓库 Ruff 零错误，`Ruff Full Repository Report` 改为阻断 | `KNOWN_GAP` | 已建立 `tools/quality_baselines/ruff.json`（基线 commit `64ed75c`、诊断集合 358 项），采用三层策略：`Ruff New Files` 阻断新增 Python 文件错误，`Ruff Baseline Regression` 阻断历史债务新增/增加，`Ruff Full Repository Report` 非阻断上传报告；历史债务在 `chore/ruff-historical-debt` 分支清理；CHANGE-20260702-010 |

## 验证状态

| 验证层级 | 状态 | 说明 |
|---|---|---|
| 本地代码/测试/文档检查 | 已完成 | pytest 1106 passed、frontend tsc/lint/build 通过、`tools/update_docs.py --check`、`check_architecture.py`、`check_docs_consistency.py`、`check_test_allowlist.py` 通过；Ruff 新增文件零错误、基线无回归 |
| GitHub Actions CI | 未验证 | 最终修复 Commit 尚未推送，Workflow 未针对最终 HEAD 运行 |
| 生产部署验证 | 未验证 | 未执行 |

> 规则：本地验证通过不能替代 CI 验证；CI 未全绿前，ALIGN-014 保持 `KNOWN_GAP`，不得进入 Phase E 或创建 release candidate。

## 关闭要求

每项关闭必须记录：发现证据、当前代码行为、修复分支、Commit、测试、生产验收、关闭日期和对应 CHANGE。关闭后只保留摘要，详细历史归入 CHANGE。
