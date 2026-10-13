/** Canvas-only ZigZag drawing; does not compute pivots or query market data. */
import { ZIGZAG_FIXED, type ZigZagOverlay, type ZigZagSegment } from './zigzag.ts'

export interface ZigZagCanvasGeometry {
  left: number
  right: number
  top: number
  bottom: number
  step: number
  /** Full calc-series index of the first visible K bar. */
  fromIndex: number
  /** Number of visible bars. */
  visibleCount: number
  font: string
  priceToY: (price: number) => number
  formatVolume: (volume: number) => string
}

function line(
  ctx: CanvasRenderingContext2D,
  seg: ZigZagSegment,
  g: ZigZagCanvasGeometry,
  projected: boolean,
): void {
  // x coordinates are in the complete calc series, NOT the zoomed slice.
  const x1 = g.left + (seg.from.index - g.fromIndex + 0.5) * g.step
  const x2 = g.left + (seg.to.index - g.fromIndex + 0.5) * g.step
  ctx.beginPath()
  ctx.setLineDash(projected ? [6, 4] : [])
  ctx.moveTo(x1, g.priceToY(seg.from.price))
  ctx.lineTo(x2, g.priceToY(seg.to.price))
  ctx.stroke()
}

function pivotLabel(
  ctx: CanvasRenderingContext2D,
  seg: ZigZagSegment,
  g: ZigZagCanvasGeometry,
  projected: boolean,
): void {
  const i = seg.to.index - g.fromIndex
  if (i < 0 || i >= g.visibleCount) return
  const x = g.left + (i + 0.5) * g.step
  const y = g.priceToY(seg.to.price)
  const isHigh = seg.to.kind === 'high'
  const col = isHigh ? '#26a69a' : '#ef5350'
  const change = seg.priceChange
  const signed = change > 0 ? '+' : ''
  // TradingView defaults: reversal price + cumulative volume + absolute delta.
  const label = `${seg.to.price.toFixed(2)}  ${g.formatVolume(seg.volume)}  ${signed}${change.toFixed(2)}`
  ctx.save()
  ctx.font = g.font
  ctx.textAlign = 'center'
  ctx.textBaseline = isHigh ? 'bottom' : 'top'
  const textW = ctx.measureText(label).width
  const padding = 4
  const boxH = 16
  const boxW = Math.min(g.right - g.left, textW + padding * 2)
  const xx = Math.max(g.left + boxW / 2, Math.min(g.right - boxW / 2, x))
  const yy = isHigh ? y - 8 - boxH : y + 8
  ctx.fillStyle = projected ? 'rgba(20,25,36,0.87)' : col
  ctx.fillRect(xx - boxW / 2, yy, boxW, boxH)
  ctx.fillStyle = '#fff'
  ctx.fillText(label, xx, isHigh ? yy + boxH - 3 : yy + 3, boxW - padding * 2)
  ctx.restore()
}

/** Draw only already-computed line/label objects. Safe to call on hover/resize. */
export function renderZigZagOverlay(
  ctx: CanvasRenderingContext2D,
  overlay: ZigZagOverlay,
  g: ZigZagCanvasGeometry,
): void {
  if (!ZIGZAG_FIXED.draw || g.visibleCount <= 0 || g.step <= 0) return
  ctx.save()
  ctx.beginPath()
  ctx.rect(g.left, g.top, g.right - g.left, g.bottom - g.top)
  ctx.clip()
  ctx.strokeStyle = ZIGZAG_FIXED.lineColor
  ctx.lineWidth = 1.6
  // Preserve timeline order. The chart clip removes segments fully offscreen.
  for (const segment of overlay.segments) line(ctx, segment, g, false)
  if (overlay.projected) line(ctx, overlay.projected, g, true)
  ctx.setLineDash([])
  // Labels are not a separate price series; they cannot influence y-domain.
  // The first pivot has no preceding swing, therefore no meaningful delta.
  const first = overlay.pivots[0]
  if (first && first.index >= g.fromIndex && first.index < g.fromIndex + g.visibleCount) {
    const x = g.left + (first.index - g.fromIndex + 0.5) * g.step
    const y = g.priceToY(first.price)
    ctx.save()
    ctx.font = g.font
    ctx.textAlign = 'center'
    ctx.textBaseline = first.kind === 'high' ? 'bottom' : 'top'
    ctx.fillStyle = first.kind === 'high' ? '#26a69a' : '#ef5350'
    ctx.fillText(first.price.toFixed(2), x, y + (first.kind === 'high' ? -5 : 5))
    ctx.restore()
  }
  for (const segment of overlay.segments) pivotLabel(ctx, segment, g, false)
  if (overlay.projected) pivotLabel(ctx, overlay.projected, g, true)
  ctx.restore()
}
