# CHANGE-20260728-006：第一金字塔视觉重构 + 小K线裁切修复 + 一级导航调整

状态：进行中
日期：2026-07-28
类型：behavior + architecture
领域：量化模型前端 / 行情体验 / 权限前端

相关 PRD：

- `../../prd/40-market-stock-experience.md`：第一金字塔状态观察、行情页右栏布局
- `../../prd/60-permissions-admin.md`：PA-10/PA-12 菜单可见性

相关 Maps：

- `../../maps/40-market-stock-experience.md` §4.1/4.2/4.3/4.4
- `../../maps/20-quant-model.md` §9.6
- `../../maps/60-permissions-admin.md` §12.5

相关提交或 PR：

- 本轮 commit（待填写 SHA）

替代：

- 部分替代 CHANGE-20260728-005 的第一金字塔 compact 视觉实现（2x2 SummaryGrid → VisualCard + 轨道 + chip）

被替代：

- 无

## 1. 摘要

本轮完成三项紧密关联的前端体验改造：第一金字塔 compact 视觉重构（删除 statusText 暴露与 2x2 SummaryGrid，改为 StateRibbon + 量能水位 + 四维 VisualCard）、小K线裁切修复（右栏固定 230px 不收缩 + 状态滚动区）、一级导航调整（行情/自选/复盘同级，自选复用 /market?scope=watchlist，删除 Toolbar 的 scopeTabs）。

## 2. 背景与问题

- 上一轮（CHANGE-20260728-005）compact 仍使用 2x2 SummaryGrid + 两列 dimensions，且暴露 `PyramidSummaryStrip(statusText)` 和原始 volume 大整数。
- `/market` 右栏小K线被下方第一金字塔压缩，导致图表底部时间轴被裁切，15m/60m/日/周/月按钮不完整。
- 旧一级导航只有「行情｜复盘」，自选作为 Toolbar 内的 scopeTab 存在，与 PRD「自选为独立一级入口」不一致；且 `MarketToolbar` 同时承担 scope 切换与筛选两类职责，耦合过重。

## 3. 变化前

- `MarketRightPanel.tsx`：无固定分区，小K线与第一金字塔共处一列，K线被压缩。
- `FirstPyramidPanel.tsx` compact：`PyramidSummaryStrip(statusText)` + 2x2 SummaryGrid + 2 列 dimensions；显示原始 volume 大整数。
- `appNavigation.ts`：`USER_NAV_ITEMS = [/market, /replay]`；自选仅通过 `MarketToolbar` 的 `scopeTabs` 切换。
- `UserAppShell.tsx`：使用 NavLink 默认 pathname 判断 active。
- `MarketToolbar.tsx`：包含 `scopeTabs/scope/onScopeChange/canAccessWatchlist` 相关 UI 和 Props。

## 4. 变化内容

### 4.1 右栏布局与小K线裁切修复

- 新建 `frontend/src/features/market-workspace/MarketRightPanel.module.scss`：
  - `.panel`：flex column, height:100%, min-height:0, overflow:hidden
  - `.klineFixed`：flex:0 0 230px, height/min-height:230px, overflow:visible
  - `.stateScroll`：flex:1 1 auto, min-height:0, overflow-y:auto, padding:0 8px 8px
- `MarketRightPanel.tsx`：固定结构 `.panel > .klineFixed(MiniKlineCard) + .stateScroll(FirstPyramidPanel compact + MoreObservation)`；MoreObservation 使用 useState 控制展开，收起时不挂载 AtomicFactsPanel。
- `global.scss`：`.mini-kline-card` 增加 flex:0 0 auto/flex-shrink:0/height 230px/overflow:visible；`.mini-kline-tabs` 固定 flex:0 0 26px；`.mini-kline-chart` 固定 flex:0 0 190px/height:190px。
- 不修改周期、K线数据 Hook、viewport 算法和图表交互。

### 4.2 第一金字塔 compact 视觉重构

- `FirstPyramidPanel.tsx`：
  - compact 禁止渲染 `PyramidSummaryStrip(statusText)`；statusText 只保留在 DTO，不进入 compact DOM。
  - 删除 2x2 SummaryGrid 和 2 列 dimensions，compact 固定单列：Header → StateRibbon → VolumeWaterLevel → TrendVisualCard → StructureVisualCard → MomentumVisualCard → ChipVisualCard。
  - StateRibbon：一行 4 个紧凑状态标签（趋势/结构/动量/筹码），高度 28px，字号 11px，title 提供完整中文。
  - TrendVisualCard：方向轨道（偏空/未确认/偏多 marker）+ 持续N根 + 距VWAP + 段量比轨道（0~2x 映射）。
  - StructureVisualCard：两个独立状态块（主要结构/短线结构）+ 事件 chips（名称/级别/方向/新鲜度，不 join）。
  - MomentumVisualCard：挤压状态 chip + 方向 chip + BB 位置轨道 + 动量变化标签 + 量价标签；原始值仅 detail 显示。
  - ChipVisualCard：POC 位置轨道（±10% 映射 clamp）+ 距离% + 峰数量；无 POC 显示灰色空态。
  - VolumeWaterLevel：20日/200日两条分位轨道，单项 null 显示「样本不足」。
  - detail 复用同一组 VisualCard，两列布局，结构跨两列，事件最多 5 条。
- `firstPyramidViewModel.ts`：新增 `vwapDeviationPct`/`segmentVolumeRatio`/`trendStrength`/`sqzmomPrev`/`bbPosition`/`momentumChangeLabel`/`distancePct`/`nPeakNodes` 等结构化展示字段，缺失即 null，不补 0，不重新计算算法。
- `FirstPyramidPanel.module.scss`：完整重写，包含轨道（track）、芯片（chip）、marker、状态标签等样式；compact 尺寸固定（padding 10px, gap 8px, 卡片 9px 10px, 圆角 6px, 标题 12px, 正文 11px）；A 股颜色（偏多红、偏空绿、中性灰），品牌莹感绿只用于量能/轨道/选中。

### 4.3 一级导航调整

- `appNavigation.ts`：
  - `USER_NAV_ITEMS` 改为 `[/market, /market?scope=watchlist, /replay]`（行情/自选/复盘）。
  - 新增 `WATCHLIST_NAV_PATH` 常量。
  - 新增 `resolveActiveNav(pathname, search, itemPath)`：按 pathname + scope 参数判断 active，不依赖 NavLink pathname。
  - 新增 `buildScopeSwitchUrl(currentParams, newScope)`：保留 keyword/industry/concept/sort/dir/filters/page_size，更新 scope，删除 selected，page 重置为 1。
- `UserAppShell.tsx`：
  - 使用 `resolveActiveNav` 判断 active；使用 `buildScopeSwitchUrl` 构建切换链接。
  - 自选导航项按 `canAccessWatchlist`（admin 或 self_selection active）过滤可见性。
- `MarketToolbar.tsx`：彻底删除 `scopeTabs/scope/onScopeChange/canAccessWatchlist` 相关 UI 和 Props，只保留股票搜索、行业、概念筛选。
- `MarketWorkspacePage.tsx`：移除传给 MarketToolbar 的 scope 相关 props。
- `appNavigation.test.ts`：更新契约测试，期望 USER_NAV_ITEMS 为 `['/market', '/market?scope=watchlist', '/replay']`。

## 5. 影响范围

- 普通用户顶栏导航从 2 项变为 3 项；自选从 Toolbar 内 scopeTab 升级为一级导航。
- `/market` 右栏小K线不再被压缩，五周期按钮和时间轴完整可见。
- compact 第一金字塔不再向普通用户暴露 statusText 和内部英文术语。
- 后端权限守卫不变；前端只调整菜单可见性与 active 判定。

## 6. 验证

- TSC：一次通过。
- ESLint（修改文件）：一次通过。
- 导航 URL 纯函数测试 `appNavigation.test.ts`：更新后期望匹配新导航项。
- 浏览器真实链路验收：待用户手工执行（本地服务保持运行）。

## 7. 未解决问题

- 浏览器真实链路验收（小K线 230px 不收缩、compact 无 statusText/英文、四维单列、行情/自选/复盘同级、切换 scope 保留筛选但清除 selected、Console 无新增错误、Network 无重复第一金字塔请求）待用户手工确认。
- ViewModel 目标测试因 Node 版本约束可能 SKIP（不安装依赖）。
