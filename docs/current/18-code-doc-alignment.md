> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 18 代码、文档与生产反馈对齐表

本表是代码基线 `6f5ae2c` 的实现事实快照，只记录“当前确认设计已经明确，但实现、测试、部署或生产表现尚未一致”的问题。未决产品问题进入 `17-open-decisions.md`；历史经过进入 CHANGE。

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
| ALIGN-010 | 飞书图片 | 生产反馈只收到文字；图片异常可被记录后接口仍返回 pending | 独立 card/image 状态、partial_failed、仅重试图片、真实 E2E | `KNOWN_GAP` | 后端单元/集成测试通过，真实渠道生产 E2E 尚未执行 |
| ALIGN-011 | Capture Token | 前端已使用独立 storage key，但尚未完成普通 API 隔离、有效期和最小权限 E2E 证据 | 最小权限、短期、独立客户端、不污染登录 | `CLOSED` | `tests/test_capture_token_isolation.py` + `tests/test_auth_login.py` 共 18 passed；代码提交 `c06a2ea` |
| ALIGN-012 | 管理页面 | 当前后端已实现邀请码、会员列表、任务和部分审计；用户启停、直接授予/续期/撤销/改套餐 API 与对应页面尚不完整，部分控件曾仅 Toast | 无真实 API 的控件删除；所有成功来自服务器结果 | `KNOWN_GAP` | 后端 admin API 与测试已覆盖，admin 生产 E2E 尚未执行 |
| ALIGN-013 | 文档旧术语 | 仓库旧文档仍出现 plan_contract、Membership、旧到期路由等 | plans + Subscription + `/subscription-expired` | `CLOSED` | 全局扫描完成；当前设计文档已统一为 plans + Subscription + `/subscription-expired`；遗留 API 路径/字段已标注 V1.6 遗留命名；见 CHANGE-20260702-005 |
| ALIGN-014 | CI | `6f5ae2c` 尚无最终 release PR 的 blocking CI 证据，且全量 Ruff/mypy 曾为非阻断 | release 候选最终 HEAD 的 blocking jobs 全绿，修改文件零新增错误 | `KNOWN_GAP` | Phase 7 完整测试与 CI 尚未执行 |
| ALIGN-015 | 运行服务 | CORE_ONLY 不包含 capture/outbox/delivery，可能造成文字有、图片无 | 部署能力与业务功能匹配；服务健康不可用时不假成功 | `KNOWN_GAP` | Phase 9 生产部署验证尚未执行 |
| ALIGN-016 | Node Cluster 输入 | `indicator_contract.py` 中 `NODE_CLUSTER_LOW_BARS=3600`，应为 250*16=4000 | 15m 输入 4000 根，1d=250，1m=2 | `KNOWN_GAP` | 待 Phase B 修复 |
| ALIGN-017 | 飞书渠道 | 运行时仍存在 `FeishuWebhookAdapter` 和 Webhook 环境变量 | Platform App only，删除全部 Webhook | `KNOWN_GAP` | 待 Phase C 修复 |
| ALIGN-018 | Capture 链路 | `stock_capture_service` 访问普通 `/stock/:symbol`，无专用 Capture API | 专用 `/capture/stock/:symbol` 路由 + Capture Token 校验 | `KNOWN_GAP` | 待 Phase C 修复 |
| ALIGN-019 | DSA 发布门禁 | `publish_run` 仍允许 partial_failed 状态发布 | partial_failed 禁止自动发布 | `KNOWN_GAP` | 待后续修复 |
| ALIGN-020 | 数量语义耦合 | `INDICATOR_BARS["15m"]=3600` 同时代表页面显示、API 返回和 Node 输入 | 拆分为 CHART_DISPLAY_BARS、INDICATOR_RESPONSE_BARS、NODE_CLUSTER_INPUT_SPEC | `KNOWN_GAP` | 待 Phase B 修复 |

## 关闭要求

每项关闭必须记录：发现证据、当前代码行为、修复分支、Commit、测试、生产验收、关闭日期和对应 CHANGE。关闭后只保留摘要，详细历史归入 CHANGE。
