// [MarketDashboard][R3A] - 共享多线图表（BreadthChart / CompareChart 唯一底层实现）
//
// 为什么必须共享：R3B/R3C/R3D 三个页面各写一套，必然出现「一页 toggle 重建 chart、
// 一页不重建」「一页保留 null gap、一页不保留」的分叉。故把 §6 硬合同固化在这里：
//   - chart / series **只创建一次**（创建签名变化才重建）；
//   - 数据只经 `setData` 更新（且仅在数据指纹变化时）；
//   - legend toggle 只走 `series.applyOptions({ visible })`（不 destroy / 不重建 / 不 setData / 不 refetch）；
//   - 因此 toggle 时 viewport / zoom / null whitespace gap 天然不变。
import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart, LineStyle, type IChartApi } from 'lightweight-charts'
import {
  buildChartTheme,
  EW_LINE_WIDTH,
  SERIES_LINE_WIDTH,
  seriesColor,
  type ChartReferenceLine,
} from './chartTheme'
import {
  createLineSeriesController,
  legendAriaPressed,
  type ChartPriceLineOptions,
  type FixedScaleRange,
  type LineChartHandle,
  type LineSeriesController,
  type LineSeriesHandle,
  type LineSeriesSpec,
  type LineWidth,
} from './lineSeriesController'
import type { LinePoint } from './dashboardLogic'
import styles from './dashboard.module.scss'

export interface MultiLineSeriesInput {
  key: string
  label: string
  /** 省略则由 shared palette 按顺序分配（普通 series 绝不使用涨跌色）。 */
  color?: string
  scale: 'left' | 'right'
  lineWidth?: LineWidth
  /** breadth 0..1 语义：左轴按百分比呈现（仅 presentation，数据保持 0..1）。 */
  breadthPercent?: boolean
  /** 固定价格轴区间（breadth 用 [0,1]），保证 0% / 100% 端点始终可见。 */
  fixedScaleRange?: FixedScaleRange
  data: LinePoint[]
}

export interface MultiLineChartProps {
  series: MultiLineSeriesInput[]
  referenceLines?: readonly ChartReferenceLine[]
  height?: number
}

/**
 * 边界适配：lightweight-charts 的 `ISeriesApi` 与纯逻辑层 `LineChartHandle` 形状不同
 * （库要求 mutable 数组 + `Time` 联合类型；纯层用 `readonly LinePoint[]` 表达 null gap 语义），
 * 二者语义等价，故在此显式桥接，避免把库类型泄漏进可单测的纯逻辑层。
 */
function toChartHandle(chart: IChartApi): LineChartHandle {
  return {
    addLineSeries: (options) => chart.addLineSeries(options) as unknown as LineSeriesHandle,
  }
}

/** FNV-1a：轻量数据指纹，用于「数据真的变了才 setData」，避免每渲染重设导致 viewport 重置。 */
function dataHash(data: readonly LinePoint[]): string {
  let h = 2166136261
  for (const point of data) {
    const raw = 'value' in point && Number.isFinite(point.value) ? String(point.value) : 'x'
    for (let i = 0; i < raw.length; i += 1) {
      h ^= raw.charCodeAt(i)
      h = Math.imul(h, 16777619)
    }
  }
  return (h >>> 0).toString(36)
}

export default function MultiLineChart({ series, referenceLines, height = 320 }: MultiLineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const controllerRef = useRef<LineSeriesController | null>(null)
  const [visibility, setVisibility] = useState<Record<string, boolean>>({})

  const specs = useMemo<LineSeriesSpec[]>(
    () =>
      series.map((s, i) => ({
        key: s.key,
        label: s.label,
        color: s.color ?? seriesColor(i),
        scale: s.scale,
        lineWidth: s.lineWidth ?? SERIES_LINE_WIDTH,
        breadthPercent: s.breadthPercent,
        fixedScaleRange: s.fixedScaleRange,
      })),
    [series],
  )

  // 轴呈现（百分比 / 固定区间）属于创建期配置：变化需要重建 series（不是 toggle 路径）。
  const creationSignature = specs
    .map(
      (s) =>
        `${s.key}|${s.scale}|${s.lineWidth}|${s.color}|${s.breadthPercent ? 'pct' : ''}|${
          s.fixedScaleRange ? `${s.fixedScaleRange.min}-${s.fixedScaleRange.max}` : ''
        }`,
    )
    .join('§')
  const dataSignature = series
    .map((s) => `${s.key}:${s.data.length}:${s.data[0]?.time ?? ''}:${s.data[s.data.length - 1]?.time ?? ''}:${dataHash(s.data)}`)
    .join('§')
  const refLinesSignature = (referenceLines ?? [])
    .map((l) => `${l.price}|${l.color}|${l.label ?? ''}|${l.dashed === false ? 'solid' : 'dashed'}`)
    .join('§')

  const dataByKeyRef = useRef<Record<string, readonly LinePoint[]>>({})
  useEffect(() => {
    dataByKeyRef.current = Object.fromEntries(series.map((s) => [s.key, s.data]))
  }, [series])

  // ---- 创建：只在创建签名 / 高度变化时重建（toggle / 数据变化都不重建） ----
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart: IChartApi = createChart(
      el,
      buildChartTheme({
        hasLeftScale: specs.some((s) => s.scale === 'left'),
        hasRightScale: specs.some((s) => s.scale === 'right'),
        height,
      }),
    )
    const controller = createLineSeriesController(toChartHandle(chart), specs)
    controller.setData(dataByKeyRef.current)

    const priceLines: ChartPriceLineOptions[] = (referenceLines ?? []).map((line) => ({
      price: line.price,
      color: line.color,
      title: line.label ?? '',
      lineWidth: 1,
      lineStyle: line.dashed === false ? LineStyle.Solid : LineStyle.Dashed,
      axisLabelVisible: true,
    }))
    controller.createPriceLines(priceLines)

    const unsubscribe = controller.onChange(() => setVisibility(controller.visibility()))
    controllerRef.current = controller
    setVisibility(controller.visibility())

    return () => {
      unsubscribe()
      controller.destroy()
      controllerRef.current = null
      chart.remove()
    }
    // 依赖仅创建签名（exhaustive-deps 无法表达「只按签名重建」的意图）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creationSignature, height, refLinesSignature])

  // ---- 数据：只在数据指纹变化时更新 ----
  // dataSignature 是 series 的纯函数指纹：避免每渲染 setData 导致 viewport/zoom 被重置。
  useEffect(() => {
    controllerRef.current?.setData(dataByKeyRef.current)
  }, [dataSignature])

  const hasLegend = specs.length > 0

  return (
    <div>
      <div ref={containerRef} className={styles.chart} style={{ height }} />
      {hasLegend && (
        <div className={styles.legend}>
          {specs.map((spec) => {
            const visible = visibility[spec.key] !== false
            return (
              <button
                key={spec.key}
                type="button"
                className={visible ? `${styles.legendItem} ${styles.legendItemOn}` : `${styles.legendItem} ${styles.legendItemOff}`}
                aria-pressed={legendAriaPressed(visible)}
                data-visible={visible ? 'true' : 'false'}
                title={visible ? `隐藏 ${spec.label}` : `显示 ${spec.label}`}
                onClick={() => controllerRef.current?.toggle(spec.key)}
              >
                <span className={styles.legendDot} style={{ background: spec.color }} aria-hidden="true" />
                <span className={styles.legendLabel}>{spec.label}</span>
                {visible ? <EyeIcon /> : <EyeOffIcon />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** 可见态图标（eye）。 */
function EyeIcon() {
  return (
    <svg className={styles.legendIcon} viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
      <path
        d="M8 3.5c3.2 0 5.6 2.2 6.5 4.5-.9 2.3-3.3 4.5-6.5 4.5S2.4 10.3 1.5 8C2.4 5.7 4.8 3.5 8 3.5Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <circle cx="8" cy="8" r="1.9" fill="currentColor" />
    </svg>
  )
}

/** 隐藏态图标（eye-off）。 */
function EyeOffIcon() {
  return (
    <svg className={styles.legendIcon} viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
      <path
        d="M2.2 8c.9-2.3 3.3-4.5 5.8-4.5 1 0 1.9.3 2.7.8M13.8 8c-.9 2.3-3.3 4.5-5.8 4.5-1 0-1.9-.3-2.7-.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeDasharray="2 1.6"
      />
      <path d="M3 13 13 3" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

export { EW_LINE_WIDTH }
