# CHANGE-20260728-005：第一金字塔双页面 UI 落地 + 邀请码前端纠正 + 生产盘后/GoAccess 只读诊断

状态：进行中  
日期：2026-07-28  
类型：behavior + incident  
领域：量化模型前端 / 权限前端 / 盘后编排 / 访问统计  

相关 PRD：

- `../../prd/40-market-stock-experience.md`：第一金字塔状态观察
- `../../prd/60-permissions-admin.md`：PA-03 邀请码周期
- `../../prd/30-after-close.md`：盘后编排

相关 Maps：

- `../../maps/40-market-stock-experience.md`
- `../../maps/60-permissions-admin.md`
- `../../maps/30-after-close.md`
- `../../maps/80-system-runtime.md`

相关 Runbooks：

- `../../runbooks/goaccess-deployment.md`

相关提交或 PR：

- 待填写（本轮 commit）

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮完成第一金字塔在 `/market` 右栏（compact）和 `/stock/:symbol` 详情 Drawer（detail）的双页面 UI 落地，抽取共享 ViewModel 和 CSS Module；纠正邀请码前端所有"自然月"残留为"周期（每周期30天）"；对生产服务器进行只读诊断，确认问软成功、盘后失败根因为 DSA StrategyRun 卡在 running 状态（非问财阻断），GoAccess 容器从未部署。

## 2. 背景与问题

- 上一轮（CHANGE-20260728-004）第一金字塔组件已支持 variant，但仍显示 algorithmVersion/Hash、原始 volume 大整数，未抽 ViewModel，样式堆积在 global.scss。
- `/market` 右栏和 `/stock` 详情页 Drawer 的双页面落点未完整验收。
- 邀请码前端仍有"自然月"文字残留（已通过精确搜索确认上一轮已修复，本轮复核）。
- 生产盘后任务连续失败，需通过 run_id 追踪真实异常。
- GoAccess 报告未生成，需确认容器/卷/日志状态。

## 3. 变化前

- `FirstPyramidPanel.tsx`：variant 支持已加，但 detail 模式仍显示 `fp-algo-version` 和 `fp-footer` Hash；趋势/动量卡显示原始 volume 大整数；样式全部在 `global.scss`；无 ViewModel；子组件命名为 `VolumeWaterLevelBar`/`SummaryCell`/`DimensionCard`/`EventItem`。
- `AdminVisitorsPage.tsx`：三段独立报错（API error / report empty / report error），本地与生产显示相同文案。
- 生产服务器：GIT_SHA=37c9fa3（origin/main HEAD），`BOARD_SYNC_ENABLED=true`。

## 4. 变化内容

### 4.1 第一金字塔双页面 UI

- 新建 `frontend/src/features/stock-research/firstPyramidViewModel.ts`：
  - 类型安全提取 `continuousFactors` 结构化字段（`regime_value`/`swing_direction`/`internal_direction`/`squeeze_on`/`current_vs_prev_volume_ratio`/`release_vs_squeeze_volume_ratio`）。
  - 禁止解析 `statusText` 推断多空或事件类型。
  - 导出 `buildFirstPyramidVM(data, variant)` 和方向/徽标辅助函数。
- 新建 `frontend/src/features/stock-research/FirstPyramidPanel.module.scss`：
  - 全部 `fp-*` 样式从 `global.scss` 迁移到 CSS Module。
  - A 股语义：偏多=`$color-up`（红），偏空=`$color-down`（绿），中性=`$color-muted`。
  - 量能水位条通过 `--vol-pct` CSS 变量驱动，避免内联 style 堆积。
- 重构 `FirstPyramidPanel.tsx`：
  - 子组件按 instruction.md 命名：`PyramidHeader`/`PyramidSummaryStrip`/`SummaryGrid`/`VolumeWaterLevel`/`TrendStateCard`/`StructureStateCard`/`MomentumStateCard`/`ChipConsensusCard`/`StructureEventList`。
  - compact 模式：顶部 2x2 摘要网格 + 量能水位 + 四维卡片，事件最多 3 条。
  - detail 模式：全宽摘要 + 量能水位 + 四维卡片（结构卡跨两列），事件最多 5 条，显示日期/价格。
  - **移除** `algorithmVersion`/`inputHash`/`parameterHash` 显示（两种模式均不显示）。
  - **移除** 原始 volume 大整数显示，仅显示 ratio（`current_vs_prev_volume_ratio`/`release_vs_squeeze_volume_ratio`）。
  - 切换股票时 `data.symbol !== symbol` 显示加载态，禁止短暂显示上一只股票状态。
- `MarketRightPanel.tsx`：MiniKlineCard 在顶部 → compact 第一金字塔 → "更多观察"（AtomicFactsPanel 默认收起）。
- `AtomicFactsDrawer.tsx`：detail 第一金字塔 → "更多状态观察"（AtomicFactsPanel expanded 默认收起）；aria-label="第一金字塔与个股状态观察"。
- `StockDetailPage.tsx`：移除页面底部独立 `FirstPyramidPanel`，全页只有一个实例（位于 Drawer 内）。
- `global.scss`：移除全部 `fp-*` 样式（已迁移到 CSS Module），保留 `.market-more-observation` 全局样式。

### 4.2 邀请码前端纠正

- `AdminUsersPage.tsx`：复核所有"自然月"/"PA-03"/"有效期月数"已替换为"周期（每周期30天）"和"PA-03：1周期=30天，按N×30天计算"。
- `endpoints.ts`：注释统一为"有效期周期数（1周期=30天）"。
- 后端 `grant_months` 优先、`grant_days` 兼容保持不变。

### 4.3 AdminVisitorsPage 空态合并

- 合并三段独立报错（API error / report empty / report error）为统一空态。
- 本地（`import.meta.env.DEV`）显示"本地不生成访问统计"。
- 生产显示"访问统计服务异常"或"访问统计报告未生成"，附操作提示。

### 4.4 生产服务器只读诊断（无代码变更）

诊断结论见 §5。

## 5. 生产服务器只读诊断结论

### 5.1 服务器版本

- GIT_SHA：`37c9fa3`（= origin/main HEAD，含 `compute_for_trade_date` 修复 #94）
- 容器均 Up 18 小时（2026-07-28 08:38:27 +0800 重启），0 restarts
- `BOARD_SYNC_ENABLED=true`

### 5.2 问财诊断

问财在最近三次盘后运行中均**成功**，不是失败原因：

| 运行 | trade_date | board_sync_result |
|---|---|---|
| 2026-07-27 16:00 | 2026-07-27 | succeeded, raw=5542, resolved=5287, 行业=257, 概念=388 |
| 2026-07-28 08:41（手动重跑） | 2026-07-27 | succeeded（同上批次） |
| 2026-07-28 16:00 | 2026-07-28 | succeeded, 行业=257, 概念=388, 关系=75511, 耗时=172519ms |

### 5.3 盘后失败根因（按 run_id 追踪）

| job_run_id | trade_date | started_at | finished_at | error |
|---|---|---|---|---|
| 3b935fae | 2026-07-27 | 07-27 16:00 | 07-27 17:16 | `compute_for_trade_date() got an unexpected keyword argument 'dsa_run_id'` |
| 24a6b39a | 2026-07-27 | 07-28 08:41 | 07-28 17:28 | `运行状态不允许发布（当前 running，仅 completed 可发布）: run_id=546911a9` |
| 52df387c | 2026-07-28 | 07-28 16:00 | 07-29 01:55 | `运行状态不允许发布（当前 running，仅 completed 可发布）: run_id=b89c01d5` |

**根因 1**（2026-07-27 16:00 运行）：`compute_for_trade_date` 接收了无效的 `dsa_run_id` 参数。此 bug 已在 origin/main `37c9fa3` 修复（PR #94），但未进入 dev 分支。

**根因 2**（2026-07-28 两次运行）：DSA StrategyRun 卡在 `running` 状态，feature snapshot 计算成功（`snapshot_count=5293, failed_count=0`），但 `publish_run` 拒绝发布（要求 `completed`）。DSA run `546911a9` 和 `b89c01d5` 均 `succeeded_count=0, failed_count=0`，说明 DSA 计算未实际执行或状态未回写。

**问软失败语义验证**：当前代码 `BOARD_SYNC_ENABLED=true` 时问财为硬依赖，但实际执行成功；失败发生在 DSA 计算和发布步骤，与问财无关。

### 5.4 GoAccess 诊断

- `docker ps -a --filter name=goaccess`：**无容器**（从未创建）
- `docker volume ls --filter name=goaccess`：**无卷**
- `trading-frontend` `/var/log/nginx/access.log` → 符号链接到 `/dev/stdout`（Docker 日志驱动，非持久文件）
- `trading-backend` `/srv/goaccess/`：**目录不存在**

结论：GoAccess 服务从未部署。docker-compose.prod.yml 定义了 goaccess 服务，但未实际启动。Nginx 日志输出到 stdout 而非共享卷，即使启动 goaccess 容器也无法读取。

建议修复命令（本轮不执行）：
1. 配置 Nginx 将 access.log 写入共享卷（不只是 stdout）
2. `docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env up -d goaccess`
3. 确认 goaccess 容器能读取 frontend access.log 卷
4. 确认 backend 容器挂载 reports 卷到 `/srv/goaccess/`

## 6. 影响范围

### 前端

- `FirstPyramidPanel.tsx` 重构（ViewModel + CSS Module + 子组件改名）
- `firstPyramidViewModel.ts` 新建
- `FirstPyramidPanel.module.scss` 新建
- `global.scss` 移除 fp-* 样式
- `MarketRightPanel.tsx` compact 第一金字塔
- `AtomicFactsDrawer.tsx` detail 第一金字塔
- `StockDetailPage.tsx` 移除底部独立 FirstPyramidPanel
- `AdminUsersPage.tsx` 邀请码文字复核
- `AdminVisitorsPage.tsx` 空态合并
- `endpoints.ts` 注释更新

### 后端

- 无变更

### Worker 与任务

- 无变更（生产诊断只读）

### 部署与运行

- 无变更（生产诊断只读）

## 7. 迁移与兼容

无迁移。前端组件重构，API 契约不变。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| TSC | 全前端 | PASS | `npx tsc --noEmit` 退出码 0 |
| ESLint | 修改文件 | PASS | 0 errors（4 pre-existing warnings in AdminUsersPage） |
| 邀请码文字 | frontend/src | PASS | `grep -r "自然月\|有效期月数" frontend/src` 无匹配 |
| 问财成功 | 生产 scheduler_job_runs metadata_json | PASS | board_sync_result.status=succeeded |
| DSA 卡住 | 生产 strategy_runs | PASS | status=running, succeeded_count=0 |
| GoAccess 未部署 | 生产 docker ps/volume ls | PASS | 无容器无卷 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | 无变化 |
| Maps | `maps/40-market-stock-experience.md` 更新第一金字塔双页面落点 |
| Runbooks | `runbooks/goaccess-deployment.md` 更新诊断方法 |
| Rules | 无变化 |

## 10. 回滚方案

前端组件重构可回滚到 8554642。生产服务器无变更，无需回滚。

## 11. 遗留问题与风险

1. **DSA StrategyRun 卡在 running 状态**：2026-07-27 和 2026-07-28 两次 DSA run 均 `succeeded_count=0, failed_count=0`，feature snapshot 计算成功但 DSA run 状态未回写为 completed。此 bug 未在 origin/main 或 dev 修复，需后续排查 `after_close_orchestrator.py:1735` 和 `strategy_batch_service.py:1132` 的状态转换逻辑。
2. **origin/main #94 未进入 dev**：`compute_for_trade_date` 修复（37c9fa3）仅在 main，dev 缺失。本轮规则禁止 merge/rebase，仅记录。
3. **GoAccess 从未部署**：需配置 Nginx 日志卷 + 启动 goaccess 容器 + 挂载 reports 卷。
4. **浏览器真实链路验收**：本轮无登录会话，需用户手工验收 `/market` 右栏 compact 第一金字塔和 `/stock/:symbol` Drawer detail 第一金字塔。

## 12. 后续变化

- 待创建：DSA StrategyRun 状态卡住修复
- 待创建：origin/main #94 cherry-pick 到 dev
- 待创建：GoAccess 容器部署
