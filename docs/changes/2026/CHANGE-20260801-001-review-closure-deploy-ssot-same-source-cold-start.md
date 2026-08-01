# CHANGE-20260801-001：盘后 Review 闭环接入 + 时间线修复 + 详情同源/DSA 旧列下线 + 冷启动展示 + 测试环境部署 SSOT

状态：已完成（部署 ff89fea；全项目收口发现的新问题由 2026-08-01 后续收口承载，不属于本记录范围）
日期：2026-08-01
类型：behavior + architecture + runtime
领域：盘后编排 / Review / 行情与个股体验 / 竞价分析 / 部署运行

## 1. 背景

2026-07-31 至 2026-08-01，围绕"盘后正式链包含 Review 阶段、列表唯一数据源、详情同源、
竞价闭环状态核验、dev SHA 测试环境部署"完成一组紧密相关的修改（`2fdb41e..ff89fea`）。
本记录汇总这些修改的行为差异与验证结果。

## 2. 修改内容（按提交）

### 2.1 `71cfc46` 盘后流水线 7 步 + computing_review + 负耗时/时区修复

- 盘后展示步骤固定为 7 步：`refreshing_daily → syncing_boards → checking_coverage
  → computing_core → computing_chip → publishing → computing_review`；
- 时间线负耗时防御：attempt 隔离 + 时区归一化（`_normalize_to_shanghai`），
  禁止跨 attempt 复用 started_at 导致负耗时。

### 2.2 `276e3d1` after-close Review 闭环（创建→计算→发布）+ 冷启动 rawValue 展示

- `after_close_orchestrator.py` 在 `publishing` 之后新增 `computing_review` 阶段：
  从 `factor_publications` 读取 stock_core / board_analysis 正式 pointer →
  `review_orchestrator_service.create_run`（幂等）→ `compute_run` → `publish_run`；
- Review 冷启动：`raw_value` 仅需当日样本即可展示，`normalized_value` 需要
  `effective_history >= 60` 交易日；`insufficient_history=true` 时前端展示 raw +
  灰态 normalized（详见 PRD 70 §25 / maps/70-review.md §21）。

### 2.3 `9be878d` 删除列表 13 个 DSA-only 旧列 + 废弃 Query4c strategy_key 路径

- 按 PRD 40 §7（MX-61）删除：趋势/连续天/VWAP差/段涨跌/斜率/强度/主要结构/
  短线结构/对齐/OB数/事件/新鲜度/动量；
- 保留基础列 + 99 个第一金字塔列；底层第一金字塔计算逻辑不变；
- 后端废弃列表读取 `strategy_key`（Query4c）路径。

### 2.4 `053a687` 详情同源：左栏 = /market/stocks 同序 + MCQ 规范查询 + 6 位代码

- 按 PRD 40 §7（MX-62）：个股详情左栏来源列表与 `/market` 列表使用同一
  `/market/stocks` 查询合同；
- URL 引入 `mcq`（Market Canonical Query，JSON 序列化的 scope/query/industry/
  concept/fp_filter/fp_sort/page/page_size 快照），禁止再传 DSA 旧格式
  `sourceRunId + canonicalQuery`；旧参数仅 deprecated 兼容解析；
- 导航锚点统一为 6 位规范 A 股代码，禁止 UUID/row index。

### 2.5 `096a5d3` 竞价 API 路由前缀补全 /api/v1

- 对齐 nginx rewrite 后的路径，修复竞价 API 404。

### 2.6 `04be202` 文档更新 + TSC 类型修正

- rules/PRD/Maps/Runbook 同步上述行为：空值语义三层合同（MX-63）、复盘闭环、
  详情同源、时间线修复、部署 SSOT；
- 前端 TSC 类型修正（无行为变更）。

### 2.7 `cf7a690` 新增 `scripts/ops/panji-test-deploy`（dev SHA 测试环境部署 SSOT）

- dev SHA 部署到腾讯云测试环境的唯一正式入口：preflight → SHA 三项精确校验
  （本地 HEAD = origin/dev = CI 全绿 SHA）→ 服务器端 image build →
  `alembic upgrade head` → 重建受影响服务 → SHA 一致性 5 项证明 → 健康检查；
- 黑名单：scp 单文件 / docker cp / 容器内手工改码 / 一次性业务脚本 / down -v。

### 2.8 `ff89fea` Review API 路径 + board_analysis 路径 + URL 合同纯净化

- Review API 与 board_analysis 读取路径对齐正式 pointer 合同；URL 合同纯净化。

## 3. 部署 SHA 一致性（2026-08-01）

- 部署入口：`scripts/ops/panji-test-deploy`；目标：腾讯云 panji-prod 测试环境；
- 部署 SHA：`ff89fea`（origin/dev）；
- 一致性核验（2026-08-01 只读核验）：repo HEAD、容器 GIT_SHA、
  `/api/v1/version` git_sha/runtime/image 四项一致 = `ff89fea`；alembic 版本 = 078；
- 验收状态：**非 CLOSURE_PASSED**。部署后分源审计确认两个真实断链
  （`fp_summary` 恒空、`fp_segment_*` 段数据断裂，见 runbooks/first-pyramid-null-audit.md
  与后续收口记录），以及 Review 发布安全、chip 生命周期等问题，均由
  2026-08-01 全项目收口任务承载，不影响本记录所述修改本身的完成状态。

## 4. 验证

- 后端目标测试（review 闭环、时间线、详情同源、DSA 列删除）在对应提交中通过；
- 前端 tsc 修正后通过；
- 部署后只读核验 SHA 四项一致（见 §3）。

## 5. 影响范围

- PRD：30-after-close §17、40-market-stock-experience §7、70-review §25、
  75-auction-analysis §8、80-system-runtime §9；
- Maps：30 §12、40 §7、70 §21、75 §9、80 §13；
- Runbook：runbooks/first-pyramid-null-audit.md（证据归档位置约定）。
