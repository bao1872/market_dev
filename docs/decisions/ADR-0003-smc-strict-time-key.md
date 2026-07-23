# ADR-0003: SMC 渲染严格 time-key 匹配

- **状态**: ACCEPTED（CP-V3-C2 @ 3393d16）
- **日期**: 2026-07-23

## 背景

SMC（Smart Money Concepts）指标在个股详情页和飞书 Capture 舞台（90 bar）渲染时，使用 `smcIdx`（SMC 计算结果中的 bar 序号）直接映射到 displayIdx（图表显示序号）。当 viewport 从 250 bar 切换到 90 bar 时，序号映射错位，导致 BOS/CHoCH/OB/EQH/EQL 结构标签位置错误。

根本原因：SMC 计算输出的 `anchor_time`/`confirmed_time`/`second_pivot_time` 是定位主键，但渲染层使用了 index fallback 而非 time 匹配。

## 决策

SMC 渲染必须使用严格 time-key 匹配（`strictTimeKey=true`）：

1. `mapSmcIndexToDisplay` 在 strict 模式下：
   - time 存在且匹配成功 → 使用 displayIdx
   - time 存在但匹配失败 → `onTimeKeyMiss('match_failed')` + skip（返回 undefined）
   - time 缺失 → `onTimeKeyMiss('missing_time')` + skip
   - 禁止 index fallback

2. events（BOS/CHoCH）和 EQH/EQL 使用 OR 逻辑：
   - anchor 和 confirmed 任一 time 匹配成功 → 渲染
   - 两者都缺失/失败 → skip

3. 详情链和 Capture（90-bar 舞台）共用同一 SMC 坐标映射核心，只允许 font/lineWidth/lane 差异。

## 影响

- `frontend/src/components/smcRendering.ts`：`SmcVisibleContext` 新增 `strictTimeKey` 和 `onTimeKeyMiss` 字段
- `frontend/src/components/StrategyChart.tsx`：`smcVisCtx` 启用 `strictTimeKey: true` + dev 诊断回调；`mapSmcEventRange` 支持 strict 模式
- time-key 匹配失败的结构不再渲染（之前会错位渲染）
- 74 个单元测试验证（11 targeted + 1 12-结构坐标验证）

## 替代方案

1. **保留 index fallback**：已否决，因为 viewport 切换时必然错位
2. **仅在 Capture 启用 strict**：已否决，因为详情链也有同样的错位风险
3. **后端返回 displayIdx**：已否决，因为 display cycle 是前端展示概念，不应进入后端计算

## 参考

- AGENTS.md §七.14 SMC FVG 完全排除 + 严格 time-key
- CHANGE-20260723-001 CP-V3-C2
- `frontend/src/components/smcRendering.ts`
- `frontend/src/components/StrategyChart.tsx`
