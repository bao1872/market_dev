// [chartViewport] - 描述: 图表视区工具，统一管理可见 K 线区间 [fromIndex, toIndex)
// 用法：供 StrategyChart 与 StockDetailPage 共享 viewport 状态，
//   避免「K线取末尾 N 根、指标取前 N 个」等数据对齐错位 bug。
//   所有图表元素（K线/DSA/BB/事件/节点）应基于同一 viewport 切片渲染。

// 视区最少 / 最多可见 bar 数（与原 displayBars 上下限一致）
// 注意：这是全局默认上限，仅供日线等常规 timeframe 使用；
// 不同 timeframe 的 range 预设可传入更宽松的 limits（见 [CHANGE-20260902]）。
export const MIN_VISIBLE_BARS = 30
export const MAX_VISIBLE_BARS = 250

// [CHANGE-20260902] 调用方可覆盖的视区上下限：
// - 15m/1h 需要可见数远超 250（如 15m 60日 = 960）
// - 月线/周线可低于 30（如月线 3月 = 3 根）
// 不全局改 30/250，避免影响所有默认缩放行为。
export interface ViewportLimits {
  min?: number
  max?: number
}

function resolveLimits(limits?: ViewportLimits): { min: number; max: number } {
  return {
    min: limits?.min ?? MIN_VISIBLE_BARS,
    max: limits?.max ?? MAX_VISIBLE_BARS,
  }
}

// 图表视区：基于完整 calc 数组的索引区间 [fromIndex, toIndex)
// - fromIndex: 起始 bar 索引（含）
// - toIndex:   结束 bar 索引（不含），等于数组长度时表示到末尾
export interface ChartViewport {
  fromIndex: number
  toIndex: number
}

function clamp(v: number, a: number, b: number): number {
  return Math.max(a, Math.min(b, v))
}

// 根据数据总量与期望可见数，构造默认 viewport（取末尾 N 根，与原 displayBars 行为一致）
// - totalBars: calc 数组长度
// - visibleCount: 期望可见 bar 数，会 clamp 到 [limits.min, min(limits.max, totalBars)]
// - limits: 可选覆盖 [min, max] 可见数限制
export function createDefaultViewport(
  totalBars: number,
  visibleCount: number = MAX_VISIBLE_BARS,
  limits?: ViewportLimits,
): ChartViewport {
  const { min, max } = resolveLimits(limits)
  const total = Math.max(0, Math.floor(totalBars))
  const upperBound = Math.max(min, Math.min(max, total))
  const visible = clamp(Math.round(visibleCount), min, upperBound)
  if (total <= min) {
    return { fromIndex: 0, toIndex: total }
  }
  const fromIndex = Math.max(0, total - visible)
  return { fromIndex, toIndex: total }
}

// clamp viewport 到 [0, totalBars] 范围，并保证最少 limits.min 根可见（数据足够时）
export function clampViewport(vp: ChartViewport, totalBars: number, limits?: ViewportLimits): ChartViewport {
  const { min } = resolveLimits(limits)
  const total = Math.max(0, Math.floor(totalBars))
  if (total === 0) return { fromIndex: 0, toIndex: 0 }
  let from = Math.max(0, Math.min(Math.floor(vp.fromIndex), total))
  let to = Math.max(from, Math.min(Math.floor(vp.toIndex), total))
  // 保证最少 limits.min 根可见（数据足够时）
  if (to - from < min) {
    if (total <= min) {
      from = 0
      to = total
    } else {
      from = Math.max(0, to - min)
      // 若 from 已贴 0 仍不足，则向前扩展 to
      if (to - from < min) {
        to = Math.min(total, from + min)
      }
    }
  }
  return { fromIndex: from, toIndex: to }
}

// [chartViewport] - 以锚点 bar 索引为中心缩放 viewport
//   zoom > 1 放大（可见数减少），< 1 缩小（可见数增加）
//   锚点在视区内的相对位置保持不变（如鼠标位置在视区 30% 处，缩放后仍位于 30%）
//   - vp: 当前 viewport
//   - anchorIndex: 锚点 bar 在 calc 数组中的绝对索引
//   - zoom: 缩放倍数
//   - totalBars: calc 数组长度
//   - limits: 可选覆盖 [min, max] 可见数限制
export function zoomAtAnchor(
  vp: ChartViewport,
  anchorIndex: number,
  zoom: number,
  totalBars: number,
  limits?: ViewportLimits,
): ChartViewport {
  const { min, max } = resolveLimits(limits)
  const total = Math.max(0, Math.floor(totalBars))
  const clamped = clampViewport(vp, total, limits)
  const visible = clamped.toIndex - clamped.fromIndex
  if (visible <= 0 || total <= min) {
    return createDefaultViewport(total, max, limits)
  }
  const maxVisible = Math.min(max, total)
  const minVisible = Math.min(min, total)
  const newVisible = clamp(Math.round(visible / zoom), minVisible, maxVisible)
  if (newVisible === visible) return clamped
  // 锚点在视区内的相对位置（0~1）
  const ratio = clamp((anchorIndex - clamped.fromIndex) / visible, 0, 1)
  let from = Math.round(anchorIndex - ratio * newVisible)
  from = Math.max(0, Math.min(from, Math.max(0, total - newVisible)))
  const to = from + newVisible
  return clampViewport({ fromIndex: from, toIndex: to }, total, limits)
}

// [chartViewport] - 平移 viewport（deltaBars > 0 向右/未来，< 0 向左/过去）
export function panViewport(
  vp: ChartViewport,
  deltaBars: number,
  totalBars: number,
  limits?: ViewportLimits,
): ChartViewport {
  const { max } = resolveLimits(limits)
  const total = Math.max(0, Math.floor(totalBars))
  const clamped = clampViewport(vp, total, limits)
  const visible = clamped.toIndex - clamped.fromIndex
  if (visible <= 0) return createDefaultViewport(total, max, limits)
  let from = clamped.fromIndex + Math.round(deltaBars)
  const maxFrom = Math.max(0, total - visible)
  from = clamp(from, 0, maxFrom)
  return clampViewport({ fromIndex: from, toIndex: from + visible }, total, limits)
}
