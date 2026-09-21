// [MarketDashboard][R3A/R3B] - Line visibility 硬合同 + 轴呈现配置（BreadthChart / CompareChart 共用唯一实现）
//
// 硬合同（R3A §6）：
//   1. chart / series **只创建一次**；
//   2. legend toggle **只**调用 `series.applyOptions({ visible })`——
//      禁止 destroy chart、禁止重建 series、禁止 setData、禁止 refetch、禁止重算 source data；
//   3. 隐藏/显示**不改变 input points**（本模块不持有、不修改调用方数据）；
//   4. 因为 toggle 不触碰 chart/data/scale，viewport / zoom / null whitespace gap
//      在 legend toggle 时天然保持不变（无需任何额外恢复逻辑）。
//
// 轴呈现（R3B §4）：breadth 源数据保持 0..1，**只**通过 series options 把左轴
// 呈现为 0%..100%（priceFormat formatter）+ 固定区间 [0,1]（autoscaleInfoProvider）。
// 绝不在数据层 ×100。
//
// 本模块刻意不依赖 React / DOM / lightweight-charts runtime（仅 type-only），
// 因此可以用 fake chart 直接断言上述调用契约。
import type { LinePoint } from './dashboardLogic'

/** 与 lightweight-charts `LineWidth` 一致（此处自带别名，保持纯逻辑层不 import 该库）。 */
export type LineWidth = 1 | 2 | 3 | 4

/** 参考线选项（lineStyle 由调用方传 lightweight-charts 的数值枚举，本模块不 import 该库）。 */
export interface ChartPriceLineOptions {
  price: number
  color: string
  title: string
  lineWidth: LineWidth
  lineStyle: number
  axisLabelVisible: boolean
}

/** breadth 0..1 → 轴标签 "0%".."100%"（仅 presentation；数据保持 0..1）。 */
export function breadthPercentFormatter(value: number): string {
  return `${Math.round(value * 100)}%`
}

/** 固定价格轴区间（breadth 用 [0,1]，保证 0%/100% 端点始终可见）。 */
export interface FixedScaleRange {
  min: number
  max: number
}

/** addLineSeries 的完整 options（纯数据，可被单测直接断言）。 */
export interface LineSeriesAddOptions {
  color: string
  lineWidth: LineWidth
  priceScaleId: string
  visible: boolean
  priceFormat?: { type: 'custom'; formatter: (price: number) => string; minMove: number }
  autoscaleInfoProvider?: () => { priceRange: { minValue: number; maxValue: number } }
}

/** lightweight-charts ISeriesApi 的最小可见子集（便于 fake 断言）。 */
export interface LineSeriesHandle {
  applyOptions(options: { visible: boolean }): void
  setData(data: readonly LinePoint[]): void
  createPriceLine(options: ChartPriceLineOptions): unknown
}

export interface LineChartHandle {
  addLineSeries(options: LineSeriesAddOptions): LineSeriesHandle
}

export interface LineSeriesSpec {
  key: string
  label: string
  color: string
  scale: 'left' | 'right'
  lineWidth: LineWidth
  /** breadth 0..1 语义：左轴按百分比呈现（**不**改数据）。 */
  breadthPercent?: boolean
  /** 固定价格轴区间（breadth 用 [0,1]），避免 autoscale 收缩导致端点不可见。 */
  fixedScaleRange?: FixedScaleRange
}

/**
 * 把纯 spec 展开为 lightweight-charts series options。
 *
 * 这是「0..1 数据 → 0..100% 轴」的唯一实现点：所有页面共用，不允许页面内再 hack 一套。
 */
export function buildSeriesOptions(spec: LineSeriesSpec): LineSeriesAddOptions {
  const options: LineSeriesAddOptions = {
    color: spec.color,
    lineWidth: spec.lineWidth,
    priceScaleId: spec.scale,
    visible: true,
  }
  if (spec.breadthPercent) {
    options.priceFormat = { type: 'custom', formatter: breadthPercentFormatter, minMove: 0.01 }
  }
  if (spec.fixedScaleRange) {
    const { min, max } = spec.fixedScaleRange
    options.autoscaleInfoProvider = () => ({ priceRange: { minValue: min, maxValue: max } })
  }
  return options
}

export interface LineSeriesController {
  readonly specs: readonly LineSeriesSpec[]
  /** 只在数据签名变化时调用；绝不重建 series。 */
  setData(dataByKey: Record<string, readonly LinePoint[]>): void
  /** 参考线同样**只创建一次**（挂在 left scale 首条线；无 left 时挂首条）。 */
  createPriceLines(lines: readonly ChartPriceLineOptions[]): void
  toggle(key: string): void
  isVisible(key: string): boolean
  visibility(): Record<string, boolean>
  onChange(listener: () => void): () => void
  destroy(): void
}

/** legend 的无障碍契约：可见/隐藏必须映射为 'true' / 'false'。 */
export function legendAriaPressed(visible: boolean): 'true' | 'false' {
  return visible ? 'true' : 'false'
}

/**
 * 创建一次 series，并返回只做 visibility 切换的控制器。
 *
 * 注意 `specs` 必须在控制器生命周期内稳定（由调用方保证：只在创建签名变化时重建控制器）。
 */
export function createLineSeriesController(
  chart: LineChartHandle,
  specs: readonly LineSeriesSpec[],
): LineSeriesController {
  const visible: Record<string, boolean> = {}
  const handles: Record<string, LineSeriesHandle> = {}
  const listeners = new Set<() => void>()

  for (const spec of specs) {
    visible[spec.key] = true
    handles[spec.key] = chart.addLineSeries(buildSeriesOptions(spec))
  }

  return {
    specs,
    setData(dataByKey) {
      for (const spec of specs) {
        const data = dataByKey[spec.key]
        if (data) handles[spec.key].setData(data)
      }
    },
    createPriceLines(lines) {
      if (lines.length === 0 || specs.length === 0) return
      const anchorSpec = specs.find((s) => s.scale === 'left') ?? specs[0]
      const anchor = handles[anchorSpec.key]
      for (const line of lines) anchor.createPriceLine(line)
    },
    toggle(key) {
      const handle = handles[key]
      if (!handle) return
      visible[key] = visible[key] === false
      // 唯一允许的 toggle 副作用（不重建、不重设数据、不重取数）。
      handle.applyOptions({ visible: visible[key] })
      for (const listener of listeners) listener()
    },
    isVisible: (key) => visible[key] !== false,
    visibility: () => ({ ...visible }),
    onChange(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    destroy() {
      listeners.clear()
    },
  }
}
