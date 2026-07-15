// [MiniKlineViewport] - 描述: 右栏小 K 线 viewport 计算纯函数（CHANGE-20260715-002 + 007）
// 解决问题：约 300px 有效绘图区塞入全部 80 根日线，左端低位 K 线被下边界裁切，
// 最新 K 线贴近右轴，周期/尺寸变化后旧 range 残留。
//
// CHANGE-20260715-007 设计要点（单一 viewport，删除 barSpacing 半实现）：
// 1. 目标根数按周期固定：15m=48、60m=44、日=40、周=36、月=30
// 2. 按实际宽度计算 barSpacing，clamp 5.5px 下界（仅用于决定 visibleBars，不应用到图表）
// 3. series.setData 只传最后 visibleBars 根，左侧负 logical range 成为真实空白
// 4. setVisibleLogicalRange({from:-2, to:clippedLength-1+3})，禁止 fitContent/rightOffset 叠加
// 5. 切周期不复用上一周期 logical range（每次重新计算）
//
// 纯 TS（无 React 依赖，无 lightweight-charts 依赖，无 @/ 别名依赖），可被 node --test 直接运行。

export type MiniKlineTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

// 右价格轴最小宽度（与 lightweight-charts v4 默认行为对齐）
export const MIN_PRICE_SCALE_WIDTH = 56

// barSpacing 下界（仅用于决定 visibleBars，不应用到图表）
const MIN_BAR_SPACING = 5.5

// 右侧留白 bar 数（最新 K 线与右轴之间保留 3 个空位）
export const RIGHT_PADDING_BARS = 3

// 左侧留白 bar 数（允许 1-2 根空数据保护）
export const LEFT_PADDING_BARS = 2

// per-timeframe 目标根数（用户指定固定值）
const TIMEFRAME_TARGET_ROOTS: Readonly<Record<MiniKlineTimeframe, number>> = {
  '15m': 48,
  '1h': 44,
  '1d': 40,
  '1w': 36,
  '1mo': 30,
}

export interface MiniKlineViewport {
  /** 实际可见 bar 数量（已按 barSpacing clamp 调整，不超过 dataLength） */
  visibleBars: number
  /** 使用的价格轴宽度 */
  priceScaleWidth: number
  /** 用于 autoscale 的有效绘图宽度（contentWidth - priceScaleWidth） */
  effectivePlotWidth: number
}

/**
 * 计算小 K 线 viewport 的可见 bar 数量。
 *
 * 算法：
 * 1. 取周期目标根数作为初始 visibleBars
 * 2. 计算 barSpacing = effectivePlotWidth / visibleBars
 * 3. 如果 barSpacing < 5.5：减少 visibleBars = floor(effectivePlotWidth / 5.5)
 * 4. visibleBars 不超过 dataLength（数据不足时显示全部）
 *
 * 注意：barSpacing 仅用于决定 visibleBars，不应用到图表（lightweight-charts 自行管理 barSpacing）。
 * 左侧/右侧留白由 computeViewportRange 的负 from 和 to 扩展实现。
 */
export function computeMiniKlineViewport(
  dataLength: number,
  timeframe: MiniKlineTimeframe,
  contentWidth: number,
): MiniKlineViewport {
  const priceScaleWidth = MIN_PRICE_SCALE_WIDTH
  const intWidth = Math.max(0, Math.floor(contentWidth))
  const effectivePlotWidth = Math.max(0, intWidth - priceScaleWidth)

  const targetRoots = TIMEFRAME_TARGET_ROOTS[timeframe]
  let visibleBars = targetRoots

  if (effectivePlotWidth > 0) {
    const barSpacing = effectivePlotWidth / visibleBars
    if (barSpacing < MIN_BAR_SPACING) {
      visibleBars = Math.max(1, Math.floor(effectivePlotWidth / MIN_BAR_SPACING))
    }
  }

  if (dataLength <= 0) {
    return { visibleBars: 0, priceScaleWidth, effectivePlotWidth }
  }

  visibleBars = Math.min(visibleBars, dataLength)
  return { visibleBars, priceScaleWidth, effectivePlotWidth }
}

/**
 * 裁剪 bars 到最后 visibleBars 根。
 * 用于 series.setData 只传可见数据，使左侧负 logical range 成为真实空白。
 *
 * @param bars 完整 K 线数据
 * @param visibleBars 可见 bar 数量
 * @returns 最后 visibleBars 根数据（新数组），若 visibleBars<=0 或数据为空则返回空数组
 */
export function clipBarsToVisible<T>(bars: readonly T[], visibleBars: number): T[] {
  if (visibleBars <= 0 || bars.length === 0) return []
  if (bars.length <= visibleBars) return [...bars]
  return bars.slice(bars.length - visibleBars)
}

/**
 * 计算单一 viewport 的 logical range。
 * from = -LEFT_PADDING_BARS（左侧 2 根空白，由负索引实现）
 * to = dataLength - 1 + RIGHT_PADDING_BARS（右侧 3 根空白）
 *
 * @param dataLength 裁剪后的数据长度（即 setData 传入的数组长度）
 * @returns { from, to } logical range
 */
export function computeViewportRange(dataLength: number): { from: number; to: number } {
  if (dataLength <= 0) return { from: 0, to: 0 }
  return {
    from: -LEFT_PADDING_BARS,
    to: dataLength - 1 + RIGHT_PADDING_BARS,
  }
}

/**
 * 计算给定数据切片的 autoscale 价格范围（min(low), max(high)）。
 * 用于 autoscaleInfoProvider 扩展可见价格范围。
 */
export function computeVisiblePriceRange<T extends { high: number; low: number }>(
  bars: readonly T[],
): { minLow: number; maxHigh: number } | null {
  if (bars.length === 0) return null
  let minLow = Infinity
  let maxHigh = -Infinity
  for (const b of bars) {
    if (b.low < minLow) minLow = b.low
    if (b.high > maxHigh) maxHigh = b.high
  }
  if (!Number.isFinite(minLow) || !Number.isFinite(maxHigh)) return null
  return { minLow, maxHigh }
}

/**
 * 计算 autoscaleInfoProvider 需要的价格范围扩展。
 * 用户指定：上方 12%，下方 15%。
 */
export function computeAutoscaleRange(
  minLow: number,
  maxHigh: number,
): { min: number; max: number } | null {
  if (!Number.isFinite(minLow) || !Number.isFinite(maxHigh)) return null
  if (minLow === maxHigh) {
    const padding = Math.abs(minLow) * 0.01 || 0.01
    return { min: minLow - padding, max: maxHigh + padding }
  }
  const range = maxHigh - minLow
  return {
    min: minLow - range * 0.15,
    max: maxHigh + range * 0.12,
  }
}
