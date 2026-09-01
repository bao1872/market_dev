// [ScopeDynamicsPanel] - 描述: Dynamics 研究面板（Slice E correction）
//
// 硬契约（prompt §2、§3、§4）：
// - 图表数据源 ONLY composition.historical_dynamics.scope_dynamics.historical_dynamics
//   （position/ema5/ema20/velocity/signal/acceleration/persistence 各自 fact-object 日期序列）。
// - 绝不前端重算 EMA/Velocity/Acceleration/Persistence/Phase。
// - 缺失观测 = whitespace 缺口（gap preservation），不填 0、不插值、不 carry。
// - Position 图固定 0–100 y 域；Velocity/Acceleration 含可见 0 参考线。
// - 当前事实来自末尾 dynamics_phase observation（persisted），绝不反推 chart series。
// - ready + phase=null → 显示 "—"，不是第七个 phase。
// - 图表渲染 lightweight-charts（例：import { createChart }），不引入新图表库。
// - 三张图均有显式标题（Position / Velocity / Acceleration），neutral analytic 线色。
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type AutoscaleInfo,
  type MouseEventParams,
} from 'lightweight-charts'
import type { ScopeDynamicsParsed } from './scopeDetailContract'
import type { ScopePhaseFact } from './types'
import {
  buildSharedTradingDates,
  alignToSharedDomain,
  chartValueAtTime,
  shouldApplyRange,
  buildPositionAutoscale,
  buildOffsetAutoscale,
  buildZeroReferenceLine,
  type Time,
  type LogicalRange,
  type ScopeDynamicsChartData,
} from './scopeDynamicsChart'
import { currentPhaseFact } from './scopeDetailContract'
import { NULL_DISPLAY, formatPosition, formatNumberNullable, formatPercentNullable, formatPhaseLabel, formatReadiness } from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import styles from './review.module.scss'

/** neutral 分析线色（非 brand green，brand green 保留给 selection/focus） */
const ANALYTIC_LINE_COLOR = '#98A1B3'
const ZERO_LINE_COLOR = 'rgba(152,161,179,0.6)'

/** 稳定空数据常量：避免 effect 依赖因每次新建 [] 而变化 */
const EMPTY_DATA: ScopeDynamicsChartData = []

interface DynamicsChartConfig {
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
 * - X 轴共享同一个 trading-date domain（buildSharedTradingDates 合并的并集），
 *   任意交易日在三图上的横向位置完全一致；
 * - Y 轴各自独立（三个独立 chart 实例）：Position 固定 0–100，
 *   Velocity/Acceleration 带 0 参考线；
 * - pan/zoom：任一图 visible logical range 变化 → 同步另外两图；
 * - crosshair：任一图 hover → 同一交易日同步高亮 + tooltip 同时显示 P/V/A；
 * - 缺失观测仍是 whitespace gap（不填 0 / 不插值 / 不 carry），tooltip 显示 "—"。
 *
 * 绝不重算 EMA / Velocity / Acceleration / Phase。
 */
function DynamicsCharts({ configs }: { configs: DynamicsChartConfig[] }) {
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

    // [P0] 统一右侧 Y 轴宽度。
    // 三张图各自自动计算 price-scale 宽度（0–100 / -x.xx / -x.xxx 标签宽度不同），
    // 会导致数据绘图区宽度不同 —— 即使 logical index 相同，屏幕 X 像素仍会错开。
    // 取三者最大宽度统一设为 minimumWidth，保证 shared logical date 映射到同一屏幕 X。
    const syncPriceScaleWidth = () => {
      const widths = charts.map((c) => c.priceScale('right').width())
      const maxWidth = Math.max(...widths)
      if (!Number.isFinite(maxWidth) || maxWidth <= 0) return
      charts.forEach((c) => {
        c.priceScale('right').applyOptions({ minimumWidth: maxWidth })
      })
    }
    syncPriceScaleWidth()

    // 容器尺寸变化后重新对齐一次（Y 轴标签宽度可能随布局改变）
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

function FactRow({ label, value }: { label: ReactNode; value: string }) {
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>{value}</span>
    </div>
  )
}

function CurrentFactStrip({ phaseFact }: { phaseFact: ScopePhaseFact | null }) {
  const f = phaseFact
  return (
    <div className={styles.factStrip}>
      {/* [REVIEW-PRODUCT-CLOSURE-01 Phase G] 状态经 formatReadiness 中文化：
          ready → 可用，不展示原始 "ready" */}
      <FactRow label={<ReviewTerm termKey="status" compact />} value={formatReadiness(f?.status)} />
      <FactRow label={<ReviewTerm termKey="phaseCurrent" compact />} value={formatPhaseLabel(f?.phase)} />
      <FactRow label={<ReviewTerm termKey="position" compact />} value={formatPosition(f?.position)} />
      <FactRow label={<ReviewTerm termKey="velocity" compact />} value={formatNumberNullable(f?.velocity)} />
      <FactRow label={<ReviewTerm termKey="acceleration" compact />} value={formatNumberNullable(f?.acceleration)} />
      <FactRow
        label={<ReviewTerm termKey="upperOccupancy" compact />}
        value={f?.upper_occupancy === null || f?.upper_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.upper_occupancy)}
      />
      <FactRow
        label={<ReviewTerm termKey="lowerOccupancy" compact />}
        value={f?.lower_occupancy === null || f?.lower_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.lower_occupancy)}
      />
    </div>
  )
}

export default function ScopeDynamicsPanel({ dynamics }: { dynamics: ScopeDynamicsParsed | null }) {
  // [Dynamics shared timeline] 三图共用同一 trading-date domain：
  // 各 series 仍保留自己的 fact-object 日期与缺失语义，仅把 X 轴位置统一到并集 domain；
  // 缺失日期在该图上仍是 whitespace gap（不填 0 / 不插值 / 不 carry）。
  // Hooks 必须无条件调用（react-hooks/rules-of-hooks），即使 dynamics 为 null。
  const sharedDates = useMemo(
    () =>
      buildSharedTradingDates(
        dynamics?.positionDates ?? [],
        dynamics?.velocityDates ?? [],
        dynamics?.accelerationDates ?? [],
      ),
    [dynamics?.positionDates, dynamics?.velocityDates, dynamics?.accelerationDates],
  )
  const positionData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.positionDates ?? [], dynamics?.position ?? []),
    [sharedDates, dynamics?.positionDates, dynamics?.position],
  )
  const velocityData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.velocityDates ?? [], dynamics?.velocity ?? []),
    [sharedDates, dynamics?.velocityDates, dynamics?.velocity],
  )
  const accelerationData = useMemo(
    () => alignToSharedDomain(sharedDates, dynamics?.accelerationDates ?? [], dynamics?.acceleration ?? []),
    [sharedDates, dynamics?.accelerationDates, dynamics?.acceleration],
  )
  if (!dynamics) {
    return <div className={styles.panelUnavailable}>本期暂无等权涨跌动态数据</div>
  }
  const current = currentPhaseFact(dynamics)
  return (
    <div className={styles.panel} data-panel="dynamics">
      <DynamicsCharts
        configs={[
          { key: 'position', title: <ReviewTerm termKey="position" compact />, data: positionData, kind: 'position', showZeroLine: false, showTimeAxis: false },
          { key: 'velocity', title: <ReviewTerm termKey="velocity" compact />, data: velocityData, kind: 'offset', showZeroLine: true, showTimeAxis: false },
          { key: 'acceleration', title: <ReviewTerm termKey="acceleration" compact />, data: accelerationData, kind: 'offset', showZeroLine: true, showTimeAxis: true },
        ]}
      />
      <CurrentFactStrip phaseFact={current} />
      {/* P1-F: TradingView Lightweight Charts 许可归属（图表 logo 已关闭，归属信息保留于此非干扰区域）。
          必须是真实 <a> 链接，不能仅为纯字符串；attributionLogo 仍保持 false（P0-3）。 */}
      <div className={styles.chartAttribution}>
        <a
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noopener noreferrer"
          className={styles.chartAttributionLink}
        >
          Charts by TradingView Lightweight Charts
        </a>
      </div>
    </div>
  )
}
