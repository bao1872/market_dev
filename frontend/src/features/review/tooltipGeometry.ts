// [tooltipGeometry] - 描述: tooltip 碰撞定位纯函数（REVIEW-UX-CLOSURE-02 P0-B）
//
// 不依赖 DOM / React，仅做几何计算，可被 node --test 直接单测。
// 给定锚点矩形、tooltip 实际渲染矩形、视口尺寸与间距，
// 返回 tooltip 的 fixed 定位 top/left，并完成 flip/clamp：
//   右侧空间不足 → 向左对齐（右边缘对齐锚点右边缘）
//   底部空间不足 → 向上偏移（显示在锚点上方）
//   顶部仍不足（tooltip 比视口高）→ 顶部 clamp 到 margin
//   左侧不足 → clamp 到 margin
//
// 使用 tooltip 真实 width/height（非固定猜测），满足 P0-B 要求。

export interface Rect {
  top: number
  left: number
  right: number
  bottom: number
  width: number
  height: number
}

export const DEFAULT_TOOLTIP_MARGIN = 8

/**
 * @param anchor 锚点元素在视口中的 getBoundingClientRect()
 * @param tooltip tooltip 实际渲染后的 getBoundingClientRect()（真实 width/height）
 * @param viewport 视口尺寸 { width, height }（通常 window.innerWidth/innerHeight）
 * @param margin 与视口边缘的最小间距
 */
export function computeTooltipPosition(
  anchor: Rect,
  tooltip: Rect,
  viewport: { width: number; height: number },
  margin: number = DEFAULT_TOOLTIP_MARGIN,
): { top: number; left: number } {
  const tooltipW = tooltip.width
  const tooltipH = tooltip.height
  const vw = viewport.width
  const vh = viewport.height

  // 默认：锚点下方、左对齐锚点左边缘
  let left = anchor.left
  let top = anchor.bottom + margin

  // 右侧空间不足 → 向左展开（右边缘对齐锚点右边缘）
  if (left + tooltipW > vw - margin) {
    left = Math.max(margin, anchor.right - tooltipW)
  }
  // 左侧仍不足 → clamp 到 margin
  if (left < margin) {
    left = margin
  }

  // 底部空间不足 → 向上偏移（显示在锚点上方）
  if (top + tooltipH > vh - margin) {
    top = anchor.top - tooltipH - margin
  }
  // 上方仍不足（tooltip 比可用高度更高）→ 顶部 clamp 到 margin
  if (top < margin) {
    top = margin
  }

  return { top, left }
}
