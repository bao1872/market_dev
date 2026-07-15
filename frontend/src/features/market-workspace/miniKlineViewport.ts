// [MiniKlineViewport] - 描述: 右栏小 K 线 viewport 计算纯函数（CHANGE-20260715-002 P0）
// 解决问题：约 300px 有效绘图区塞入全部 80 根日线，左端低位 K 线被下边界裁切，
// 最新 K 线贴近右轴，周期/尺寸变化后旧 range 残留。
//
// 设计要点（CHANGE-20260715-002 用户指定）：
// 1. 目标根数按周期固定：15m=48、60m=44、日=40、周=36、月=30
// 2. 按实际宽度计算 barSpacing，clamp 5.5–8px
// 3. 左侧 1–2 根留白：from=max(-2, n-visible-1)，右侧 3 根留白：to=n-1+3
// 4. 不调用 fitContent、resetTimeScale 或 scrollToRealTime 覆盖 range
// 5. 切周期不得沿用上一周期 logical range（每次重新计算）
//
// 纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。

export type MiniKlineTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

// 右价格轴最小宽度（与 lightweight-charts v4 默认行为对齐）
export const MIN_PRICE_SCALE_WIDTH = 56

// barSpacing clamp 区间（用户指定 5.5–8px）
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
  /** 实际可见 bar 数量（已按 barSpacing clamp 调整） */
  visibleBars: number
  /** 逻辑 range from 索引（含左侧 1-2 根留白） */
  from: number
  /** 逻辑 range to 索引（含右侧 3 bar 留白） */
  to: number
  /** 使用的价格轴宽度 */
  priceScaleWidth: number
  /** 用于 autoscale 的有效绘图宽度（contentWidth - priceScaleWidth） */
  effectivePlotWidth: number
  /** 每根 bar 占用的水平像素（barSpacing） */
  barSpacing: number
}

/**
 * 计算小 K 线 viewport 的可见区间与价格轴宽度。
 *
 * 算法：
 * 1. 取周期目标根数作为初始 visibleBars
 * 2. 计算 barSpacing = effectivePlotWidth / visibleBars
 * 3. 如果 barSpacing < 5.5：减少 visibleBars = floor(effectivePlotWidth / 5.5)
 * 4. 如果 barSpacing > 8：保持目标根数（更宽的 bar 间距可接受，不强制增加根数）
 * 5. visibleBars 不超过 dataLength（数据不足时显示全部）
 * 6. from = max(-LEFT_PADDING_BARS, dataLength - visibleBars - 1)
 * 7. to = dataLength - 1 + RIGHT_PADDING_BARS
 *
 * @param dataLength 数据总长度（bars 数组长度）
 * @param timeframe 当前周期
 * @param contentWidth 容器内容宽度（像素，应为 ResizeObserver contentRect.width 的整数值）
 * @returns viewport 信息
 */
export function computeMiniKlineViewport(
  dataLength: number,
  timeframe: MiniKlineTimeframe,
  contentWidth: number,
): MiniKlineViewport {
  const priceScaleWidth = MIN_PRICE_SCALE_WIDTH
  // 整数化 contentWidth（避免亚像素导致 visible bars 抖动）
  const intWidth = Math.max(0, Math.floor(contentWidth))
  const effectivePlotWidth = Math.max(0, intWidth - priceScaleWidth)

  // 周期目标根数
  const targetRoots = TIMEFRAME_TARGET_ROOTS[timeframe]

  // 初始 visibleBars = 目标根数
  let visibleBars = targetRoots

  // 计算 barSpacing，clamp 5.5-8px
  if (effectivePlotWidth > 0) {
    let barSpacing = effectivePlotWidth / visibleBars
    if (barSpacing < MIN_BAR_SPACING) {
      // barSpacing 太窄，减少根数
      visibleBars = Math.max(1, Math.floor(effectivePlotWidth / MIN_BAR_SPACING))
      barSpacing = effectivePlotWidth / visibleBars
    }
    // barSpacing > 8 时不增加根数（保持目标根数，更宽间距可接受）
  }

  // 数据为空时返回零区间
  if (dataLength <= 0) {
    return {
      visibleBars: 0,
      from: 0,
      to: 0,
      priceScaleWidth,
      effectivePlotWidth,
      barSpacing: 0,
    }
  }

  // visibleBars 不超过 dataLength（数据不足时显示全部）
  visibleBars = Math.min(visibleBars, dataLength)

  // from = max(-LEFT_PADDING_BARS, dataLength - visibleBars - 1)
  // 左侧 1-2 根留白（允许负值，lightweight-charts 支持显示空数据区）
  const from = Math.max(-LEFT_PADDING_BARS, dataLength - visibleBars - 1)
  // to = dataLength - 1 + RIGHT_PADDING_BARS（右侧 3 bar 留白）
  const to = dataLength - 1 + RIGHT_PADDING_BARS

  // 最终 barSpacing
  const barSpacing = effectivePlotWidth > 0 && visibleBars > 0
    ? effectivePlotWidth / visibleBars
    : 0

  return {
    visibleBars,
    from,
    to,
    priceScaleWidth,
    effectivePlotWidth,
    barSpacing,
  }
}

/**
 * 计算给定数据切片的 autoscale 价格范围（min(low), max(high)）。
 * 用于 autoscaleInfoProvider 扩展可见价格范围。
 *
 * @param bars K 线数据切片（仅可见部分）
 * @returns { minLow, maxHigh } 或 null（数据为空）
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
 *
 * @param minLow 可见 K 线最低价
 * @param maxHigh 可见 K 线最高价
 * @returns 扩展后的 { min, max } 或 null
 */
export function computeAutoscaleRange(
  minLow: number,
  maxHigh: number,
): { min: number; max: number } | null {
  if (!Number.isFinite(minLow) || !Number.isFinite(maxHigh)) return null
  if (minLow === maxHigh) {
    // 价格无波动时，人为扩展 1% 范围
    const padding = Math.abs(minLow) * 0.01 || 0.01
    return { min: minLow - padding, max: maxHigh + padding }
  }
  const range = maxHigh - minLow
  // 上方 12%，下方 15%
  return {
    min: minLow - range * 0.15,
    max: maxHigh + range * 0.12,
  }
}
