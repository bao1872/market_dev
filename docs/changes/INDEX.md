# Change Index

## 1. 使用说明

本文件只用于查找 Change，不复制正文。

状态和验证结论以具体 Change 文件为准。

## 2. 变更索引

| Change ID | 日期 | 标题 | 类型 | 领域 | 状态 | 关联 PRD | 关联 Maps |
|---|---|---|---|---|---|---|---|
| CHANGE-20260802-005 | 2026-08-02 | 治理去角色化：删除工具专属规则（60-trae-work / 70-trae-cn），收敛为通用执行主体合同 | governance | 治理规则/检查器（业务代码零改动） | 生效（规则与检查器已改；`needs_deploy = false`） | — | `rules/40-testing-quality.md`、`rules/50-git-development-flow.md`、`rules/80-deployment-data-safety.md`、`rules/90-deprecated-forbidden.md` |
| CHANGE-20260802-003 | 2026-08-02 | 部署执行入口收敛为两脚本（Live Mount）+ CI 降级为手工诊断工具 + Change ID 治理修复；§6 补充首次 Live Mount 实跑前的 6 项缺陷修正 | architecture+ops+governance | 部署脚本/GitHub Actions/部署纪律（业务代码零改动） | 生效（`verified_code`：脚本与 workflow 已改，本地合同 67+23+15 全通过；`needs_deploy`：尚未在 panji-prod 实跑） | SR-01～SR-62 | `rules/80-deployment-data-safety.md`、`maps/80-system-runtime.md`、`runbooks/development-deployment.md` |
| CHANGE-20260802-002 | 2026-08-02 | 竞价归入复盘权限 + 邀请码权限回显 | behavior/contract | 权限/前端 | `verified_code`（后端 32 + 前端 35 单测、TSC、ESLint 本地复验通过）/ `runtime_pending`（生产链路未核验） | PA-12/PA-20/PA-21 | `maps/60-permissions-admin.md` §11 |
| CHANGE-20260801-003 | 2026-08-01 | 全项目问题收口状态 | behavior/architecture | 全局 | 进行中 | PRD20/40/70/75 | `maps/20/40/70/75` |
| CHANGE-20260726-001 | 2026-07-26 | 文档体系重构 | architecture | 文档治理 | 进行中 | PRD/Maps 全局 | 全局 |
| CHANGE-20260726-002 | 2026-07-26 | 本地与远程运行模型 | runtime | 运行体系 | 历史（运行事实；部署相关结论已被 2026-08-02 治理收口更新） | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260726-003 | 2026-07-26 | 权限模型拆分 | behavior | 权限 | 进行中 | PA-01～PA-31 | `maps/60-permissions-admin.md` |
| CHANGE-20260726-004 | 2026-07-26 | （历史记录，已并入 001） | architecture | 文档治理 | superseded | — | — |
| CHANGE-20260727-001 | 2026-07-27 | 分支治理 | runtime | 运行体系 | 进行中 | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260727-002 | 2026-07-27 | 盘后日线 readiness 修复 | behavior | 盘后 | 进行中 | AC-04 | `maps/30-after-close.md` |
| CHANGE-20260727-003 | 2026-07-27 | 仓库边界与本地运行 | runtime | 运行体系 | 进行中 | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260727-004 | 2026-07-27 | 第一金字塔本地根路由 | behavior | 量化模型 | 进行中 | QM-01～QM-43 | `maps/20-quant-model.md` |
| CHANGE-20260727-005 | 2026-07-27 | Phase 5B-2 权限模型与部署脚本 | behavior | 权限/部署 | 历史（权限模型已落地；部署脚本部分已被 2026-08-02 治理收口 supersede） | PA-01～PA-31 | `maps/60-permissions-admin.md`、`maps/80-system-runtime.md` |
| CHANGE-20260727-006 | 2026-07-27 | Gate 2 权限代码（require_any_capability + 邀请码三勾选 + UI gating） | behavior | 权限 | 进行中（代码+单元测试+DB 集成测试通过，真实运行未核验） | PA-01/PA-02/PA-10/PA-11/PA-13/PA-20 | `maps/60-permissions-admin.md` §12 |
| CHANGE-20260728-001 | 2026-07-28 | 盘中结构事件图片按事件独立截图（修复"有文字无图片"） | bugfix | 盘中监控 | 进行中（代码+测试通过，真实盘中运行未核验） | WI-12 | `maps/50-watchlist-intraday.md` §4 |
| CHANGE-20260728-002 | 2026-07-28 | 盘迹一轮收口（Gate 1/3/4/5 代码+验证） | architecture | 量化模型/盘后/管理后台 | 进行中（代码+源码级验证通过，Gate2 真实验收+运行时受本地约束阻塞） | PRD20/PRD30/PRD60 | `maps/20-quant-model.md`、`maps/30-after-close.md`、`maps/60-permissions-admin.md`、`maps/80-system-runtime.md` |
| CHANGE-20260728-003 | 2026-07-28 | 本地登录恢复 + 邀请码 30 天周期 + 第一金字塔定稿 | behavior | 权限/量化模型/安全 | 进行中（代码+目标测试通过，浏览器验收待启动服务） | PA-03/QM-01~QM-43/QM-60~QM-62 | `maps/60-permissions-admin.md`、`maps/20-quant-model.md` |
| CHANGE-20260728-004 | 2026-07-28 | 本地数据架构纠正 + 永久禁用测试库 + de7fbcb 遗留修复 | architecture+bugfix | 运行体系/权限/量化模型/安全 | 进行中（代码+目标测试通过，浏览器真实链路验收待完成） | SR-03/SR-40/PA-03/QM-01~QM-43 | `maps/80-system-runtime.md`、`maps/60-permissions-admin.md`、`maps/20-quant-model.md` |
| CHANGE-20260728-005 | 2026-07-28 | 第一金字塔双页面 UI 落地 + 邀请码前端纠正 + 生产盘后/GoAccess 只读诊断 | behavior+incident | 量化模型前端/权限前端/盘后编排/访问统计 | 进行中（代码+TSC+ESLint通过，浏览器验收待用户手工） | PRD40/QM-01~QM-43/PA-03/PRD30 | `maps/40-market-stock-experience.md`、`maps/30-after-close.md`、`maps/60-permissions-admin.md`、`maps/80-system-runtime.md` |
| CHANGE-20260728-006 | 2026-07-28 | 第一金字塔视觉重构 + 小K线裁切修复 + 一级导航调整 | behavior+architecture | 量化模型前端/行情体验/权限前端 | 进行中（代码+TSC+ESLint通过，浏览器验收待用户手工） | PRD40/QM-01~QM-43/PA-10/PA-12 | `maps/40-market-stock-experience.md`、`maps/20-quant-model.md`、`maps/60-permissions-admin.md` |
| CHANGE-20260728-008 | 2026-07-28 | 统一盘后编排 + 列表视图 99 字段 + 永久删除持久测试库 | architecture+feature+security | 盘后编排/行情体验/测试基础设施 | 进行中（代码+目标测试通过，待 commit + push dev + CI + merge main + 部署 + 生产运行） | AC-16/MX-20/SR-03 | `maps/30-after-close.md`、`maps/40-market-stock-experience.md`、`maps/80-system-runtime.md` |
| CHANGE-20260728-010 | 2026-07-28 | 盘中监控双类别 + 飞书固定组合图 | behavior+architecture | 盘中监控/飞书截图/行情体验 | 进行中（P0 补丁已合入 2026-07-29：swing_bias 类型修复+sendStockDetailFeishu 简化+captureReady 纯函数；浏览器真实链路验收待用户手工） | WI-02/WI-12/MX-30 | `maps/50-watchlist-intraday.md`、`maps/40-market-stock-experience.md`、`maps/20-quant-model.md` |
| CHANGE-20260729-003 | 2026-07-29 | 第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦 | architecture+behavior+bugfix | 量化模型/盘后编排/筛选器 | 进行中（代码+目标纯单元测试+Ruff 通过；浏览器验收待手工；chip 持久化 migration 为下一阶段唯一 blocker） | QM-01~QM-43/QM-60~QM-62/AC-04 | `maps/20-quant-model.md`、`maps/30-after-close.md` |
| CHANGE-20260729-004 | 2026-07-29 | 第一金字塔 99 字段服务端筛选排序 + 筹码共识结构化 chipStatus | behavior+architecture | 行情体验/量化模型 | 进行中（代码+目标测试+TSC+ESLint 通过，浏览器真实链路验收待用户手工） | MX-20/QM-01~QM-43 | `maps/40-market-stock-experience.md`、`maps/20-quant-model.md` |
| CHANGE-20260729-005 | 2026-07-29 | 99字段真实筛选排序修复 + GoAccess logrotate/healthcheck + 部署脚本补 goaccess | bugfix+architecture | 行情体验/量化模型/运维 | 进行中（代码+纯单元测试61+Ruff+TSC+ESLint 通过；CI 待查询；浏览器验收待手工） | MX-20/QM-01~QM-43 | `maps/40-market-stock-experience.md`、`maps/20-quant-model.md`、`maps/80-system-runtime.md` |
| CHANGE-20260729-006 | 2026-07-29 | 盘后编排与历史回补增量检查点/分层发布重构 | architecture+behavior+contract | 盘后编排/历史回补/分层发布 | 历史（代码事实；"分层发布"流程术语已被 2026-08-02 治理收口弃用，增量发布实现仍属历史） | AC-08/09/10/14/QM-01~QM-43 | `maps/30-after-close.md`、`maps/20-quant-model.md` |
| CHANGE-20260729-007 | 2026-07-29 | 增量发布真实接入收口 + ID 合同修复 + 个股自选按钮微缩 | architecture+bugfix+contract+UI | 盘后编排/分层发布/ID 合同/个股详情 | 历史（代码事实；发布流程术语已弃用） | AC-08/09/10/14/MX-40~MX-43 | `maps/30-after-close.md`、`maps/40-market-stock-experience.md` |
| CHANGE-20260729-008 | 2026-07-29 | 增量发布最终收口：Worker 接入 run items + market_stocks pointer + history DB-only CLI + 管理 API + 聚合独立 job | architecture+contract | 盘后编排/分层发布/历史回补/管理后台 | 历史（代码事实；发布流程术语已弃用） | AC-08/09/10/14/MX-20/MX-40~MX-43 | `maps/30-after-close.md`、`maps/40-market-stock-experience.md` |
| CHANGE-20260729-009 | 2026-07-29 | 行情列表统一数据源 + 内联自选按钮 + 筹码原因 + History版本一致性 + Umami访客分析 | architecture+behavior+ops | 行情体验/量化模型/运维 | 进行中（代码+25+8 单元测试+Ruff+TSC+ESLint 通过；Umami 已部署运行；待 dev→main→生产部署验收） | MX-20/MX-40~MX-43/AC-04 | `maps/40-market-stock-experience.md`、`maps/30-after-close.md`、`maps/80-system-runtime.md` |
| CHANGE-20260730-012 | 2026-07-30 | P0 收口—盘中监控1秒/结构图片/第一金字塔/列表排序/全市场行情扫描/实时K线修复/复盘基线 | behavior+contract+architecture+data | 盘中监控/行情体验/量化模型/盘后编排/复盘 | 进行中（代码+目标纯单元测试293+Ruff+TSC+ESLint 通过；PG 集成待 CI；浏览器验收待用户手工） | WI-02/WI-12/MX-20/QM-01~QM-43/AC-04/RV-01~RV-05 | `maps/50-watchlist-intraday.md`、`maps/40-market-stock-experience.md`、`maps/20-quant-model.md`、`maps/30-after-close.md`、`maps/70-review.md` |
| CHANGE-20260730-013 | 2026-07-30 | 复盘工作台 V1 完整实现 + 第一金字塔 symbol 合同 P0 修复 | architecture+behavior+contract+data | 复盘模块/量化模型/行情体验/盘后编排/部署 | 进行中（代码+部署+canary 已发布；migration 076 已应用；adapter/chip_status/review run 验证通过；浏览器 UI 真实链路验收 PENDING 用户手工登录）【修正于 014：review-1.0.0 仅为代码骨架部署，数据验收失败（history 基线未接入、scope_key 合同错误、force 发布不可用数据）】 | QM-01~QM-43/RV-01~RV-22/MX-20/MX-40~MX-43 | `maps/70-review.md`、`maps/20-quant-model.md`、`maps/40-market-stock-experience.md`、`maps/30-after-close.md`、`maps/80-system-runtime.md` |
| CHANGE-20260730-014 | 2026-07-30 | P0 复盘数据链+行情缺口+盘后恢复+99字段筛选+第一金字塔折叠 | behavior+contract+architecture+data | 复盘模块/行情质量/盘后编排/行情体验/量化模型 | 进行中（代码已合入 main SHA 54fe3a2；review-1.1.0 修复仅静态核验，canary review run 重跑待生产 SSH 可达；浏览器 UI 真实链路验收 PENDING 用户手工登录） | RV-01~RV-22（§23 P0 强化条款）/MQ-01~MQ-40/MX-50~MX-53/AC-12~AC-14 | `maps/70-review.md`、`maps/30-after-close.md`、`maps/40-market-stock-experience.md`、`maps/10-market-data.md`、`prd/50-market-data-quality.md`、`runbooks/market-data-quality-scan-repair.md` |
| CHANGE-20260730-015 | 2026-07-30 | SSH 目标漂移防复发治理 | architecture+governance | 部署运维/TRAE 工作协议/生产安全 | 生效（scripts/ops/panji-prod-ssh + panji-prod-preflight 已落库并实际验证通过；preflight 三阶段全部 OK） | 无（运维治理） | `maps/80-system-runtime.md` §2 |
| CHANGE-20260730-016 | 2026-07-30 | 盘后链路 P0 永久收口 + 新模型合同冻结 | behavior+contract+architecture+data | 盘后编排/复盘模块/行情质量/量化模型/部署运维 | 进行中（代码+目标纯单元测试+Ruff 通过；PG 集成待 CI；本轮未部署、未 push main、未修改生产数据；canary 计算已完成静态核验但未改生产） | AC-04/AC-08~14/RV-01~RV-22/AA-01~AA-NN | `maps/30-after-close.md`、`maps/70-review.md`、`maps/75-auction-analysis.md`、`runbooks/after-close-recovery.md` |
| CHANGE-20260730-017 | 2026-07-30 | 发布前真实闭环 — 状态机+部署合同+CI门禁+基线对齐 | behavior+contract+architecture+ci | 盘后编排/部署运维/复盘模块/CI 治理/测试基础设施 | **历史（superseded）**：其中 Release Gate / CI 门禁部署前置 / 正式发布流程已被 2026-08-02 治理收口废止；分层发布业务实现仍属历史事实 | AC-04/AC-08~14/RV-01~RV-22 | `maps/30-after-close.md`、`maps/70-review.md`、`maps/80-system-runtime.md` |
| CHANGE-20260730-018 | 2026-07-30 | 竞价分析完整链路 — 锚点+扫描+聚合+追踪+前端 | behavior+contract+architecture+data | 竞价分析/盘后编排/第二金字塔/复盘模块/前端 | 进行中（代码+测试+前端已实现；PG集成待CI；canary和部署待后续） | AU-01~AU-16/AC-01~AC-20/RV-01~RV-12 | `maps/75-auction-analysis.md`、`maps/30-after-close.md`、`maps/70-review.md` |
| CHANGE-20260731-001 | 2026-07-31 | Auction Scheduler 真实可达 + 09:25 数据源审计 | behavior+architecture+data | 竞价分析/盘后编排/生产运行 | **仍 BLOCKED**（AUCTION_DATA_SOURCE_BLOCKED + BLOCKED_NO_STAGING + CI 终态未确认） | AU-01~AU-16 | `maps/75-auction-analysis.md`、`maps/80-system-runtime.md`、`runbooks/auction-analysis.md` |
| CHANGE-20260801-001 | 2026-08-01 | 盘后 Review 闭环 + 时间线修复 + 详情同源/DSA 旧列下线 + 冷启动展示 + 测试环境部署 SSOT | behavior+architecture+runtime | 盘后/Review/行情个股/竞价/部署 | 已完成（部署 ff89fea，验收非 CLOSURE_PASSED，新问题由后续收口承载） | AC/ MX-60~63 / Review 冷启动 / AU / 部署 SSOT | `maps/30-after-close.md`、`maps/40-market-stock-experience.md`、`maps/70-review.md`、`maps/75-auction-analysis.md`、`maps/80-system-runtime.md` |
| CHANGE-20260801-002 | 2026-08-01 | 全项目问题收口候选版本 | behavior+contract+architecture+data+ci | Review/第一金字塔/板块/行情体验/竞价/治理 | 候选代码完成；最终 SHA CI/PG 待验证；竞价外部双源阻断；生产零写入 | 收口总任务书 | `2026/CHANGE-20260801-002-full-project-closure.md` |
| CHANGE-20260802-001 | 2026-08-02 | dev-only 分支治理 + 区间筛选双输入 + Review bootstrap 正式入口 + 部署脚本/资源门禁修复 | governance+behavior+architecture+contract+ops | 分支模型/行情筛选/Review 回填/盘后 Worker/部署运维 | 历史（代码已合入 dev；部署脚本/SSOT 部分已被 2026-08-02 治理收口（CHANGE-20260802-003）supersede） | RV-24.4/RV-25-03/SR-09/SR-10/MX 区间筛选 | `maps/70-review.md`、`rules/80-deployment-data-safety.md` |
| CHANGE-20260802-002 | 2026-08-02 | 竞价归入复盘权限 + 邀请码权限回显 | behavior+contract | 竞价/权限 | `verified_code` / `runtime_pending`；生产外部竞价真值源仍阻断 | AU/PA | `maps/75-auction-analysis.md`、`maps/60-permissions-admin.md` |
| CHANGE-20260802-003 | 2026-08-02 | 部署入口收敛、手工 CI 与 Live Mount 当前合同；§6 首次 Live Mount 实跑缺陷修正 | architecture+ops+governance | 部署脚本/CI/规则/Runbook | `verified_code`（本地合同全通过）/ `needs_deploy`（未执行生产部署） | SR-12~14/TQ-90/DS-90~93 | `maps/80-system-runtime.md`、`runbooks/development-deployment.md` |
| CHANGE-20260802-004 | 2026-08-02 | CI 三层重构（Fast CI / Release Gate / Nightly）+ 部署脚本结构重构 + 测试分类收口 | architecture+ci+ops | CI 工作流/部署脚本/测试分类（业务代码零改动） | **历史（superseded）**：Release Gate / GHCR / 三层 CI / CI Gate 部署门禁流程已被 CHANGE-20260802-003 废止；CI 仅保留为手工诊断工具。（原编号 CHANGE-20260802-002，因撞号重编为 004） | TQ-90~TQ-93 / DS-90~DS-93 | `rules/40-testing-quality.md`、`rules/80-deployment-data-safety.md`、`maps/80-system-runtime.md` §13/§14 |
| CHANGE-20260802-005 | 2026-08-02 | 治理去角色化与单一规则/检查入口 | governance | AGENTS/rules/checker | 代码与本地合同待最终验证；无需部署或 migration | 通用执行主体合同 | `rules/README.md`、`rules/50-git-development-flow.md` |

## 3. 状态说明

以上 Change 记录的是已经确认的重要方向。

在代码、数据和运行状态完成核验前，不得改为“已完成”。

已被后续 Change 取代的历史记录标记为 `superseded`，保留以供追溯。
