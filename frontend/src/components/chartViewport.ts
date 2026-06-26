// [chartViewport] - 描述: 图表视区工具，统一管理可见 K 线区间 [fromIndex, toIndex)
// 用法：供 StrategyChart 与 StockDetailPage 共享 viewport 状态，
//   避免「K线取末尾 N 根、指标取前 N 个」等数据对齐错位 bug。
//   所有图表元素（K线/DSA/BB/事件/节点）应基于同一 viewport 切片渲染。

// 视区最少 / 最多可见 bar 数（与原 displayBars 上下限一致）
export const MIN_VISIBLE_BARS = 30
export const MAX_VISIBLE_BARS = 250

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
// - visibleCount: 期望可见 bar 数，会 clamp 到 [MIN, min(MAX, totalBars)]
export function createDefaultViewport(
  totalBars: number,
  visibleCount: number = MAX_VISIBLE_BARS,
): ChartViewport {
  const total = Math.max(0, Math.floor(totalBars))
  const upperBound = Math.max(MIN_VISIBLE_BARS, Math.min(MAX_VISIBLE_BARS, total))
  const visible = clamp(Math.round(visibleCount), MIN_VISIBLE_BARS, upperBound)
  if (total <= MIN_VISIBLE_BARS) {
    return { fromIndex: 0, toIndex: total }
  }
  const fromIndex = Math.max(0, total - visible)
  return { fromIndex, toIndex: total }
}

// clamp viewport 到 [0, totalBars] 范围，并保证最少 MIN_VISIBLE_BARS 根可见（数据足够时）
export function clampViewport(vp: ChartViewport, totalBars: number): ChartViewport {
  const total = Math.max(0, Math.floor(totalBars))
  if (total === 0) return { fromIndex: 0, toIndex: 0 }
  let from = Math.max(0, Math.min(Math.floor(vp.fromIndex), total))
  let to = Math.max(from, Math.min(Math.floor(vp.toIndex), total))
  // 保证最少 MIN_VISIBLE_BARS 根可见（数据足够时）
  if (to - from < MIN_VISIBLE_BARS) {
    if (total <= MIN_VISIBLE_BARS) {
      from = 0
      to = total
    } else {
      from = Math.max(0, to - MIN_VISIBLE_BARS)
      // 若 from 已贴 0 仍不足，则向前扩展 to
      if (to - from < MIN_VISIBLE_BARS) {
        to = Math.min(total, from + MIN_VISIBLE_BARS)
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
export function zoomAtAnchor(
  vp: ChartViewport,
  anchorIndex: number,
  zoom: number,
  totalBars: number,
): ChartViewport {
  const total = Math.max(0, Math.floor(totalBars))
  const clamped = clampViewport(vp, total)
  const visible = clamped.toIndex - clamped.fromIndex
  if (visible <= 0 || total <= MIN_VISIBLE_BARS) {
    return createDefaultViewport(total)
  }
  const maxVisible = Math.min(MAX_VISIBLE_BARS, total)
  const minVisible = Math.min(MIN_VISIBLE_BARS, total)
  const newVisible = clamp(Math.round(visible / zoom), minVisible, maxVisible)
  if (newVisible === visible) return clamped
  // 锚点在视区内的相对位置（0~1）
  const ratio = clamp((anchorIndex - clamped.fromIndex) / visible, 0, 1)
  let from = Math.round(anchorIndex - ratio * newVisible)
  from = Math.max(0, Math.min(from, Math.max(0, total - newVisible)))
  const to = from + newVisible
  return clampViewport({ fromIndex: from, toIndex: to }, total)
}

// [chartViewport] - 平移 viewport（deltaBars > 0 向右/未来，< 0 向左/过去）
export function panViewport(
  vp: ChartViewport,
  deltaBars: number,
  totalBars: number,
): ChartViewport {
  const total = Math.max(0, Math.floor(totalBars))
  const clamped = clampViewport(vp, total)
  const visible = clamped.toIndex - clamped.fromIndex
  if (visible <= 0) return createDefaultViewport(total)
  let from = clamped.fromIndex + Math.round(deltaBars)
  const maxFrom = Math.max(0, total - visible)
  from = clamp(from, 0, maxFrom)
  return clampViewport({ fromIndex: from, toIndex: from + visible }, total)
}
