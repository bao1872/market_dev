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
import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { createChart, type IChartApi, type ISeriesApi, type AutoscaleInfo, type IPriceLine } from 'lightweight-charts'
import type { ScopeDynamicsParsed } from './scopeDetailContract'
import type { ScopePhaseFact } from './types'
import {
  alignDynamicsSeries,
  buildPositionAutoscale,
  buildOffsetAutoscale,
  buildZeroReferenceLine,
  type ScopeDynamicsChartData,
} from './scopeDynamicsChart'
import { currentPhaseFact } from './scopeDetailContract'
import { NULL_DISPLAY, formatPosition, formatNumberNullable, formatPercentNullable, formatPhaseLabel, formatReadiness } from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import styles from './review.module.scss'

/** neutral 分析线色（非 brand green，brand green 保留给 selection/focus） */
const ANALYTIC_LINE_COLOR = '#98A1B3'
const ZERO_LINE_COLOR = 'rgba(152,161,179,0.6)'

function useInlineChart(
  containerRef: React.RefObject<HTMLDivElement | null>,
  data: ScopeDynamicsChartData,
  kind: 'position' | 'offset',
  showZeroLine: boolean,
) {
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart: IChartApi = createChart(el, {
      height: 120,
      // P0-3: 关闭 Lightweight Charts 自带 attribution logo（"TV"）。
      // 许可要求的归属信息移至面板底部非干扰区域（见 chartAttribution），不再出现在图表左下角。
      layout: {
        background: { color: 'transparent' },
        textColor: '#98A1B3',
        fontFamily: 'SFMono-Regular, monospace',
        attributionLogo: false,
      },
      grid: { vertLines: { color: 'rgba(38,52,64,0.35)' }, horzLines: { color: 'rgba(38,52,64,0.35)' } },
      rightPriceScale: { borderColor: '#263440' },
      timeScale: { borderColor: '#263440', timeVisible: false },
      crosshair: { mode: 0 },
    })
    const series: ISeriesApi<'Line'> = chart.addLineSeries({
      color: ANALYTIC_LINE_COLOR,
      lineWidth: 1,
      autoscaleInfoProvider: (base: AutoscaleInfo | null) => {
        const range = kind === 'position' ? buildPositionAutoscale(data) : buildOffsetAutoscale(data)
        if (!range) return base
        return { priceRange: { minValue: range.min, maxValue: range.max }, margins: base?.margins }
      },
    })
    series.setData(
      data.map((p) =>
        'value' in p ? { time: p.time, value: p.value } : ({ time: p.time } as never),
      ),
    )
    if (showZeroLine) {
      const priceLine: IPriceLine = series.createPriceLine(buildZeroReferenceLine(ZERO_LINE_COLOR))
      // priceLine 在组件卸载时由 chart.remove() 清理
      void priceLine
    }
    chart.timeScale().fitContent()
    return () => {
      chart.remove()
    }
    // containerRef 为稳定 ref，不会导致 effect 重跑；data/kind/showZeroLine 变化才重建图表。
    // title 为 ReactNode（ReviewTerm），不参与 effect 依赖，避免 JSX element 引用变化触发重建。
  }, [data, kind, showZeroLine, containerRef])
}

function SeriesChart({
  data,
  kind,
  title,
  showZeroLine,
}: {
  data: ScopeDynamicsChartData
  kind: 'position' | 'offset'
  title: ReactNode
  showZeroLine: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  useInlineChart(ref, data, kind, showZeroLine)
  return (
    <div className={styles.dynamicsChartWrapper} data-chart-kind={kind}>
      <div className={styles.dynamicsChartTitle}>{title}</div>
      <div ref={ref} className={styles.dynamicsChart} />
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
  // 每个 series 使用自己的 fact-object 日期（never compressed，不静默截断）
  // Hooks 必须无条件调用（react-hooks/rules-of-hooks），即使 dynamics 为 null
  const positionData = useMemo(
    () => alignDynamicsSeries(dynamics?.positionDates ?? [], dynamics?.position ?? []),
    [dynamics?.positionDates, dynamics?.position],
  )
  const velocityData = useMemo(
    () => alignDynamicsSeries(dynamics?.velocityDates ?? [], dynamics?.velocity ?? []),
    [dynamics?.velocityDates, dynamics?.velocity],
  )
  const accelerationData = useMemo(
    () => alignDynamicsSeries(dynamics?.accelerationDates ?? [], dynamics?.acceleration ?? []),
    [dynamics?.accelerationDates, dynamics?.acceleration],
  )
  if (!dynamics) {
    return <div className={styles.panelUnavailable}>本期暂无收益动态数据</div>
  }
  const current = currentPhaseFact(dynamics)
  return (
    <div className={styles.panel} data-panel="dynamics">
      <SeriesChart data={positionData} kind="position" title={<ReviewTerm termKey="position" compact />} showZeroLine={false} />
      <SeriesChart data={velocityData} kind="offset" title={<ReviewTerm termKey="velocity" compact />} showZeroLine />
      <SeriesChart data={accelerationData} kind="offset" title={<ReviewTerm termKey="acceleration" compact />} showZeroLine />
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
