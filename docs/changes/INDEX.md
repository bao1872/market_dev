# Change Index

## 1. 使用说明

本文件只用于查找 Change，不复制正文。

状态和验证结论以具体 Change 文件为准。

## 2. 变更索引

| Change ID | 日期 | 标题 | 类型 | 领域 | 状态 | 关联 PRD | 关联 Maps |
|---|---|---|---|---|---|---|---|
| CHANGE-20260726-001 | 2026-07-26 | 文档体系重构 | architecture | 文档治理 | 进行中 | PRD/Maps 全局 | 全局 |
| CHANGE-20260726-002 | 2026-07-26 | 本地与远程运行模型 | runtime | 运行体系 | 进行中 | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260726-003 | 2026-07-26 | 权限模型拆分 | behavior | 权限 | 进行中 | PA-01～PA-31 | `maps/60-permissions-admin.md` |
| CHANGE-20260726-004 | 2026-07-26 | （历史记录，已并入 001） | architecture | 文档治理 | superseded | — | — |
| CHANGE-20260727-001 | 2026-07-27 | 分支治理 | runtime | 运行体系 | 进行中 | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260727-002 | 2026-07-27 | 盘后日线 readiness 修复 | behavior | 盘后 | 进行中 | AC-04 | `maps/30-after-close.md` |
| CHANGE-20260727-003 | 2026-07-27 | 仓库边界与本地运行 | runtime | 运行体系 | 进行中 | SR-01～SR-62 | `maps/80-system-runtime.md` |
| CHANGE-20260727-004 | 2026-07-27 | 第一金字塔本地根路由 | behavior | 量化模型 | 进行中 | QM-01～QM-43 | `maps/20-quant-model.md` |
| CHANGE-20260727-005 | 2026-07-27 | Phase 5B-2 权限模型与部署脚本 | behavior | 权限/部署 | 进行中 | PA-01～PA-31 | `maps/60-permissions-admin.md`、`maps/80-system-runtime.md` |
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
| CHANGE-20260729-006 | 2026-07-29 | 盘后编排与历史回补增量检查点/分层发布重构 | architecture+behavior+contract | 盘后编排/历史回补/分层发布 | 进行中（代码+目标纯单元测试27+Ruff 通过；PG 集成测试待 CI；浏览器验收待手工） | AC-08/09/10/14/QM-01~QM-43 | `maps/30-after-close.md`、`maps/20-quant-model.md` |
| CHANGE-20260729-007 | 2026-07-29 | 增量发布真实接入收口 + ID 合同修复 + 个股自选按钮微缩 | architecture+bugfix+contract+UI | 盘后编排/分层发布/ID 合同/个股详情 | PARTIAL（071 FK 修复+073 NOT NULL+is_stale 真源+stock_context pointer 接入+UI 微缩；纯单元27+Ruff+TSC+ESLint 通过；浏览器验收 AUTH_WALL_BLOCKED；Worker/market_stocks/history 真实接入未完成） | AC-08/09/10/14/MX-40~MX-43 | `maps/30-after-close.md`、`maps/40-market-stock-experience.md` |
| CHANGE-20260729-008 | 2026-07-29 | 增量发布最终收口：Worker 接入 run items + market_stocks pointer + history DB-only CLI + 管理 API + 聚合独立 job | architecture+contract | 盘后编排/分层发布/历史回补/管理后台 | 进行中（代码闭环完成+纯单元27+Ruff+TSC+ESLint 通过；PG 集成待 CI；待 dev→main→部署→canary→全量回补） | AC-08/09/10/14/MX-20/MX-40~MX-43 | `maps/30-after-close.md`、`maps/40-market-stock-experience.md` |

## 3. 状态说明

以上 Change 记录的是已经确认的重要方向。

在代码、数据和运行状态完成核验前，不得改为“已完成”。

已被后续 Change 取代的历史记录标记为 `superseded`，保留以供追溯。
