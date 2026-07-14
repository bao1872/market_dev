// [MiniKlineViewport] - 描述: 右栏小 K 线 viewport 计算纯函数（CHANGE-011 P0）
// 解决问题：约 300px 有效绘图区塞入全部 80 根日线，左端低位 K 线被下边界裁切，
// 最新 K 线贴近右轴，周期/尺寸变化后旧 range 残留。
//
// 设计要点：
// 1. 初始可见根数按宽度动态计算 floor((contentWidth - priceScaleWidth) / 5)，clamp 到 per-timeframe 区间
// 2. 右侧保留约 3 个 bar 空位（最新 K 线不得紧贴价格轴）
// 3. 数据可继续多取用于缓存，但初始可见根数由 viewport 决定
// 4. 切周期不得沿用上一周期 logical range（每次重新计算）
//
// per-timeframe clamp（用户指定，全部在 [30, 64] 区间内）：
//   15m/60m: 50–64
//   日线:    48–58
//   周线:    40–52
//   月线:    30–40
//
// 纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。

export type MiniKlineTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

// 右价格轴最小宽度（与 lightweight-charts v4 默认行为对齐）
export const MIN_PRICE_SCALE_WIDTH = 56

// 每个 bar 占用的最小水平像素数（含间隙）
const MIN_PX_PER_BAR = 5

// 右侧留白 bar 数（最新 K 线与右轴之间保留 3 个空位）
export const RIGHT_PADDING_BARS = 3

// per-timeframe clamp 区间
const TIMEFRAME_CLAMP: Record<MiniKlineTimeframe, Readonly<[number, number]>> = {
  '15m': [50, 64],
  '1h': [50, 64],
  '1d': [48, 58],
  '1w': [40, 52],
  '1mo': [30, 40],
}

export interface MiniKlineViewport {
  /** 实际可见 bar 数量（已 clamp） */
  visibleBars: number
  /** 逻辑 range from 索引（含左侧空数据保护） */
  from: number
  /** 逻辑 range to 索引（含右侧 3 bar 留白） */
  to: number
  /** 使用的价格轴宽度 */
  priceScaleWidth: number
  /** 用于 autoscale 的有效绘图宽度（contentWidth - priceScaleWidth） */
  effectivePlotWidth: number
}

function clamp(value: number, min: number, max: number): number {
  if (value < min) return min
  if (value > max) return max
  return value
}

/**
 * 计算小 K 线 viewport 的可见区间与价格轴宽度。
 *
 * @param dataLength 数据总长度（bars 数组长度）
 * @param timeframe 当前周期
 * @param contentWidth 容器内容宽度（像素，应为 ResizeObserver contentRect.width 的整数值）
 * @returns viewport 信息（visibleBars/from/to/priceScaleWidth/effectivePlotWidth）
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

  // 基础可见根数 = floor(effectiveWidth / 5)
  const rawVisible = Math.floor(effectivePlotWidth / MIN_PX_PER_BAR)

  // per-timeframe clamp（全部在 [30, 64] 区间内）
  const [minBars, maxBars] = TIMEFRAME_CLAMP[timeframe]
  const visibleBars = clamp(rawVisible, minBars, maxBars)

  // 数据为空时返回零区间
  if (dataLength <= 0) {
    return {
      visibleBars: 0,
      from: 0,
      to: 0,
      priceScaleWidth,
      effectivePlotWidth,
    }
  }

  // from = max(0, dataLength - visibleBars)（左侧数据不足时从头开始）
  const from = Math.max(0, dataLength - visibleBars)
  // to = dataLength - 1 + RIGHT_PADDING_BARS（右侧保留 3 bar 空位）
  // 注意：lightweight-charts logical range 的 to 是开区间右端，但 setVisibleLogicalRange 文档
  //   实际使用闭区间语义；我们用 dataLength - 1 + 3 让最后一根 K 线出现在约 80% 位置
  const to = dataLength - 1 + RIGHT_PADDING_BARS

  return {
    visibleBars,
    from,
    to,
    priceScaleWidth,
    effectivePlotWidth,
  }
}

/**
 * 计算给定数据切片的 autoscale 价格范围（min(low), max(high)）。
 * 用于校验价格轴 scaleMargins 后的可见范围是否覆盖所有可见 K 线的影线。
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
