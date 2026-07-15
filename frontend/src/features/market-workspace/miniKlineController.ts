// [MiniKlineController] - 描述: 右栏小 K 线图表生命周期控制器（CHANGE-20260715-007）
// 将 MiniKlineCard 的图表创建/数据更新/resize/销毁逻辑提取为可注入依赖的纯逻辑模块，
// 使其可在 Node.js --test 环境下用 mock createChart + mock rAF 进行真实行为测试。
//
// 设计要点：
// 1. 不在模块顶层 import React 或 lightweight-charts（仅类型导入会被 --experimental-strip-types 剥离）
// 2. createChart 和 raf 通过 deps 注入，测试时传 mock，生产时传真实实现
// 3. 图表选项（配色/高度/价格轴等）硬编码在此模块，测试可通过 mock createChart 的调用参数断言
// 4. setData 只传最后 visibleBars 根（clipBarsToVisible），range 使用 computeViewportRange 固定 {from:-2, to:n-1+3}
// 5. 切 symbol/timeframe/width 时取消旧 rAF，重新 setData + range
// 6. 禁止 fitContent / resetTimeScale / scrollToRealTime

import {
  computeMiniKlineViewport,
  clipBarsToVisible,
  computeViewportRange,
  computeAutoscaleRange,
  MIN_PRICE_SCALE_WIDTH,
  type MiniKlineTimeframe,
} from './miniKlineViewport.ts'

// ===== 最小 chart API 接口（结构兼容 lightweight-charts v4，无需 import lightweight-charts）=====

export interface CandleBar {
  time: string | number
  open: number
  high: number
  low: number
  close: number
}

interface AutoscaleInfo {
  priceRange: { minValue: number; maxValue: number } | null
}

interface SeriesOptionsInternal {
  autoscaleInfoProvider?: (baseImpl: () => AutoscaleInfo | null) => AutoscaleInfo | null
  [key: string]: unknown
}

interface SeriesApiInternal {
  setData(data: CandleBar[]): void
  applyOptions(options: SeriesOptionsInternal): void
}

interface TimeScaleApiInternal {
  setVisibleLogicalRange(range: { from: number; to: number }): void
  applyOptions(options: Record<string, unknown>): void
}

interface ChartApiInternal {
  addCandlestickSeries(options: SeriesOptionsInternal): SeriesApiInternal
  timeScale(): TimeScaleApiInternal
  applyOptions(options: Record<string, unknown>): void
  remove(): void
}

type CreateChartFn = (
  container: HTMLElement,
  options: Record<string, unknown>,
) => ChartApiInternal

interface RafProvider {
  request: (cb: () => void) => number
  cancel: (id: number) => void
}

// ===== 常量 =====

const CHART_HEIGHT = 190

const INTRADAY_TIMEFRAMES: ReadonlySet<MiniKlineTimeframe> = new Set(['15m', '1h'])

// ===== Controller =====

export interface MiniKlineController {
  /** 更新数据 + 周期，重新裁剪 + setData + 应用 range */
  setData(bars: CandleBar[], timeframe: MiniKlineTimeframe, width: number): void
  /** 容器宽度变化，重新裁剪 + setData + 应用 range */
  resize(width: number): void
  /** 销毁：取消 pending rAF + chart.remove() */
  destroy(): void
}

export interface MiniKlineControllerDeps {
  createChart: CreateChartFn
  raf: RafProvider
}

/**
 * 创建小 K 线图表控制器。
 *
 * @param container DOM 容器元素
 * @param initialWidth 初始宽度（px）
 * @param deps 依赖注入：createChart 函数 + raf provider
 * @returns controller 接口
 */
export function createMiniKlineController(
  container: HTMLElement,
  initialWidth: number,
  deps: MiniKlineControllerDeps,
): MiniKlineController {
  const { createChart, raf } = deps

  // ColorType.Solid = "solid" in lightweight-charts v4
  const chart = createChart(container, {
    layout: {
      background: { type: 'solid', color: '#0A0F14' },
      textColor: '#98A1B3',
      fontSize: 11,
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: '#161F29' },
      horzLines: { color: '#161F29' },
    },
    rightPriceScale: {
      borderColor: '#263440',
      minimumWidth: MIN_PRICE_SCALE_WIDTH,
      autoScale: true,
      scaleMargins: { top: 0.08, bottom: 0.08 },
    },
    timeScale: {
      borderColor: '#263440',
      timeVisible: false,
      secondsVisible: false,
      shiftVisibleRangeOnNewBar: false,
    },
    crosshair: { mode: 0 },
    width: initialWidth,
    height: CHART_HEIGHT,
  })

  const series = chart.addCandlestickSeries({
    upColor: '#FF4D4F',
    downColor: '#22C55E',
    borderUpColor: '#FF4D4F',
    borderDownColor: '#22C55E',
    wickUpColor: '#FF4D4F',
    wickDownColor: '#22C55E',
    autoscaleInfoProvider: (baseImpl: () => AutoscaleInfo | null) => {
      const base = baseImpl()
      if (!base || !base.priceRange) return base ?? null
      const { minValue, maxValue } = base.priceRange
      const extended = computeAutoscaleRange(minValue, maxValue)
      if (!extended) return base
      return {
        ...base,
        priceRange: { minValue: extended.min, maxValue: extended.max },
      }
    },
  })

  let currentBars: CandleBar[] = []
  let currentTimeframe: MiniKlineTimeframe = '1d'
  let rafId: number | null = null

  function applyRange(width: number): void {
    const vp = computeMiniKlineViewport(currentBars.length, currentTimeframe, width)
    if (vp.visibleBars <= 0) return
    const clipped = clipBarsToVisible(currentBars, vp.visibleBars)
    series.setData(clipped)
    const range = computeViewportRange(clipped.length)
    chart.timeScale().setVisibleLogicalRange(range)
  }

  function scheduleApply(width: number): void {
    if (rafId !== null) {
      raf.cancel(rafId)
      rafId = null
    }
    rafId = raf.request(() => {
      rafId = null
      applyRange(width)
    })
  }

  return {
    setData(bars: CandleBar[], timeframe: MiniKlineTimeframe, width: number): void {
      currentBars = bars
      currentTimeframe = timeframe
      chart.applyOptions({
        timeScale: { timeVisible: INTRADAY_TIMEFRAMES.has(timeframe) },
      })
      if (width > 0) {
        scheduleApply(width)
      }
    },

    resize(width: number): void {
      chart.applyOptions({ width })
      scheduleApply(width)
    },

    destroy(): void {
      if (rafId !== null) {
        raf.cancel(rafId)
        rafId = null
      }
      chart.remove()
    },
  }
}
