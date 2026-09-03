// [ScopeDynamicsCharts] - 描述: Position / Velocity / Acceleration 三图共享 renderer
//
// [SLICE 4 / Price] 从 ScopeDynamicsPanel 抽出的“三图 renderer”窄块，供
// ScopeDynamicsPanel 与 ScopePriceAnalysisPanel 复用。
// 抽出而非复制：不复制整套 chart engine，旧 ScopeDynamicsPanel 行为完全不变。
//
// 硬契约（沿用 Dynamics 原契约）：
// - 数据来自 persisted Composition historical_dynamics（fact-object 日期序列），
//   本组件绝不重算 EMA / Velocity / Acceleration / Phase / Position。
// - 缺失观测 = whitespace 缺口（gap preservation），不填 0、不插值、不 carry。
// - Position 固定 0–100 y 域；Velocity / Acceleration 含 0 参考线。
// - 三图共享交易日 X 轴 + 共享 crosshair。
import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type AutoscaleInfo,
  type MouseEventParams,
} from 'lightweight-charts'
import {
  chartValueAtTime,
  shouldApplyRange,
  buildPositionAutoscale,
  buildOffsetAutoscale,
  buildZeroReferenceLine,
  type Time,
  type LogicalRange,
  type ScopeDynamicsChartData,
} from './scopeDynamicsChart'
import { NULL_DISPLAY, formatPosition, formatNumberNullable } from './reviewFormat'
import styles from './review.module.scss'

/** neutral 分析线色（非 brand green，brand green 保留给 selection/focus） */
const ANALYTIC_LINE_COLOR = '#98A1B3'
const ZERO_LINE_COLOR = 'rgba(152,161,179,0.6)'

/** 稳定空数据常量：避免 effect 依赖因每次新建 [] 而变化 */
const EMPTY_DATA: ScopeDynamicsChartData = []

export interface DynamicsChartConfig {
  key: 'position' | 'velocity' | 'acceleration'
  title: ReactNode
  data: ScopeDynamicsChartData
  kind: 'position' | 'offset'
  showZeroLine: boolean
  /** 是否显示公共日期轴（仅最下方 Acceleration 显示，避免三份重复刻度） */
  showTimeAxis: boolean
}

/**
 * [Dynamics shared timeline] 三图统一时间横截面。
 *
 * - X 轴共享同一个 trading-date domain，任意交易日在三图上的横向位置完全一致；
 * - Y 轴各自独立：Position 固定 0–100，Velocity/Acceleration 带 0 参考线；
 * - pan/zoom 与 crosshair 三图同步；
 * - 缺失观测仍是 whitespace gap（不填 0 / 不插值 / 不 carry），tooltip 显示 "—"。
 *
 * 绝不重算 EMA / Velocity / Acceleration / Phase。
 */
export default function DynamicsCharts({ configs }: { configs: DynamicsChartConfig[] }) {
  const posRef = useRef<HTMLDivElement>(null)
  const velRef = useRef<HTMLDivElement>(null)
  const accRef = useRef<HTMLDivElement>(null)
  const [hoveredDate, setHoveredDate] = useState<string | null>(null)

  const positionData = configs[0]?.data ?? EMPTY_DATA
  const velocityData = configs[1]?.data ?? EMPTY_DATA
  const accelerationData = configs[2]?.data ?? EMPTY_DATA

  useEffect(() => {
    const refs = [posRef.current, velRef.current, accRef.current]
    if (refs.some((el) => !el) || configs.length !== 3) return

    const charts: IChartApi[] = []
    const seriesList: ISeriesApi<'Line'>[] = []
    let syncing = false

    configs.forEach((cfg, idx) => {
      const el = refs[idx]
      if (!el) return
      const chart: IChartApi = createChart(el, {
        height: 120,
        layout: {
          background: { color: 'transparent' },
          textColor: '#98A1B3',
          fontFamily: 'SFMono-Regular, monospace',
          attributionLogo: false,
        },
        grid: { vertLines: { color: 'rgba(38,52,64,0.35)' }, horzLines: { color: 'rgba(38,52,64,0.35)' } },
        rightPriceScale: { borderColor: '#263440' },
        timeScale: { borderColor: '#263440', timeVisible: false, visible: cfg.showTimeAxis },
        crosshair: { mode: 0 },
      })
      const series: ISeriesApi<'Line'> = chart.addLineSeries({
        color: ANALYTIC_LINE_COLOR,
        lineWidth: 1,
        autoscaleInfoProvider: (base: AutoscaleInfo | null) => {
          const range = cfg.kind === 'position' ? buildPositionAutoscale(cfg.data) : buildOffsetAutoscale(cfg.data)
          if (!range) return base
          return { priceRange: { minValue: range.min, maxValue: range.max }, margins: base?.margins }
        },
      })
      series.setData(
        cfg.data.map((pt) =>
          'value' in pt ? { time: pt.time as Time, value: pt.value } : ({ time: pt.time as Time } as never),
        ),
      )
      if (cfg.showZeroLine) {
        series.createPriceLine(buildZeroReferenceLine(ZERO_LINE_COLOR))
      }
      charts.push(chart)
      seriesList.push(series)
    })

    if (charts.length !== 3) return

    // lightweight-charts 的 subscribe* 返回 void，注销需传回原 handler
    const rangeHandlers = charts.map((chart, i) => {
      const handler = (range: LogicalRange | null) => {
        if (!shouldApplyRange(range, syncing)) return
        syncing = true
        charts.forEach((other, j) => {
          if (j !== i) other.timeScale().setVisibleLogicalRange(range)
        })
        syncing = false
      }
      chart.timeScale().subscribeVisibleLogicalRangeChange(handler)
      return handler
    })

    const crossHandlers = charts.map((chart, i) => {
      const handler = (param: MouseEventParams) => {
        const time = (param.time ?? null) as string | null
        setHoveredDate(time)
        if (syncing) return
        if (!time) {
          // [P1] 鼠标离开图表 / 事件位置不可用（Lightweight Charts 4.2 mouse-leave）：
          // 必须清除另外两张图上的 programmatic crosshair，否则 tooltip 已复位
          // 而另外两张图仍停在上一交易日。
          charts.forEach((other, j) => {
            if (j !== i) other.clearCrosshairPosition()
          })
          return
        }
        syncing = true
        charts.forEach((other, j) => {
          if (j === i) return
          const v = chartValueAtTime(configs[j].data, time)
          if (v === null) {
            other.clearCrosshairPosition()
          } else {
            other.setCrosshairPosition(v, time as Time, seriesList[j])
          }
        })
        syncing = false
      }
      chart.subscribeCrosshairMove(handler)
      return handler
    })

    charts.forEach((c) => c.timeScale().fitContent())

    // [P0] 统一右侧 Y 轴宽度，保证 shared logical date 映射到同一屏幕 X。
    const syncPriceScaleWidth = () => {
      const widths = charts.map((c) => c.priceScale('right').width())
      const maxWidth = Math.max(...widths)
      if (!Number.isFinite(maxWidth) || maxWidth <= 0) return
      charts.forEach((c) => {
        c.priceScale('right').applyOptions({ minimumWidth: maxWidth })
      })
    }
    syncPriceScaleWidth()

    const resizeObserver =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(() => syncPriceScaleWidth())
    if (resizeObserver) {
      const el = refs[0]
      if (el) resizeObserver.observe(el)
    }

    return () => {
      if (resizeObserver) resizeObserver.disconnect()
      charts.forEach((c, i) => {
        c.timeScale().unsubscribeVisibleLogicalRangeChange(rangeHandlers[i])
        c.unsubscribeCrosshairMove(crossHandlers[i])
      })
      charts.forEach((c) => c.remove())
    }
  }, [positionData, velocityData, accelerationData, configs])

  const hv = hoveredDate
    ? {
        position: chartValueAtTime(positionData, hoveredDate),
        velocity: chartValueAtTime(velocityData, hoveredDate),
        acceleration: chartValueAtTime(accelerationData, hoveredDate),
      }
    : null

  return (
    <div className={styles.dynamicsChartsRoot}>
      {configs.map((cfg, idx) => (
        <div key={cfg.key} className={styles.dynamicsChartWrapper} data-chart-kind={cfg.kind}>
          <div className={styles.dynamicsChartTitle}>{cfg.title}</div>
          <div ref={idx === 0 ? posRef : idx === 1 ? velRef : accRef} className={styles.dynamicsChart} />
        </div>
      ))}
      <div className={styles.dynamicsTooltip} data-hover={hoveredDate ? 'active' : 'idle'} aria-live="polite">
        {hoveredDate && hv ? (
          <>
            <div className={styles.dynamicsTooltipDate}>{hoveredDate}</div>
            <div className={styles.dynamicsTooltipRow}>
              <span>等权涨跌分位</span>
              <span>{hv.position === null ? NULL_DISPLAY : formatPosition(hv.position)}</span>
            </div>
            <div className={styles.dynamicsTooltipRow}>
              <span>分位动能</span>
              <span>{formatNumberNullable(hv.velocity)}</span>
            </div>
            <div className={styles.dynamicsTooltipRow}>
              <span>动能偏离</span>
              <span>{formatNumberNullable(hv.acceleration)}</span>
            </div>
          </>
        ) : (
          <span className={styles.dynamicsTooltipHint}>悬停查看同一交易日的分位 / 动能 / 偏离</span>
        )}
      </div>
    </div>
  )
}
