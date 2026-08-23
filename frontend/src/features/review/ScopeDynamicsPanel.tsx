// [ScopeDynamicsPanel] - 描述: Dynamics 研究面板（Slice E）
//
// 硬契约（prompt §5、§6、§20）：
// - 图表数据源 ONLY composition.historical_dynamics.scope_dynamics.historical_dynamics
//   （position/ema5/ema20/velocity/signal/acceleration/persistence 日期对齐序列）。
// - 绝不前端重算 EMA/Velocity/Acceleration/Persistence/Phase。
// - 缺失观测 = whitespace 缺口（gap preservation），不填 0、不插值、不 carry。
// - Position 图固定 0–100 y 域；Velocity/Acceleration 含 0 参考线。
// - 当前事实来自末尾 dynamics_phase observation（persisted），绝不反推 chart series。
// - ready + phase=null → 显示 "—"，不是第七个 phase。
// - 图表渲染 lightweight-charts（例：import { createChart }），不引入新图表库。
import { useEffect, useMemo, useRef } from 'react'
import { createChart, type IChartApi, type ISeriesApi, type AutoscaleInfo } from 'lightweight-charts'
import type { ScopeDynamicsParsed } from './scopeDetailContract'
import type { ScopePhaseFact } from './types'
import {
  alignDynamicsSeries,
  buildPositionAutoscale,
  buildOffsetAutoscale,
  type ScopeDynamicsChartData,
} from './scopeDynamicsChart'
import { currentPhaseFact } from './scopeDetailContract'
import { NULL_DISPLAY, formatPosition, formatNumberNullable, formatPercentNullable, formatPhaseLabel } from './reviewFormat'
import styles from './review.module.scss'

function useInlineChart(
  containerRef: React.RefObject<HTMLDivElement | null>,
  data: ScopeDynamicsChartData,
  kind: 'position' | 'offset',
) {
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart: IChartApi = createChart(el, {
      height: 120,
      layout: { background: { color: 'transparent' }, textColor: '#98A1B3', fontFamily: 'SFMono-Regular, monospace' },
      grid: { vertLines: { color: 'rgba(38,52,64,0.35)' }, horzLines: { color: 'rgba(38,52,64,0.35)' } },
      rightPriceScale: { borderColor: '#263440' },
      timeScale: { borderColor: '#263440', timeVisible: false },
      crosshair: { mode: 0 },
    })
    const series: ISeriesApi<'Line'> = chart.addLineSeries({
      color: '#00F6C2',
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
    chart.timeScale().fitContent()
    return () => {
      chart.remove()
    }
    // containerRef 为稳定 ref，不会导致 effect 重跑；显式纳入依赖以对齐 hooks 规范
  }, [data, kind, containerRef])
}

function SeriesChart({ data, kind }: { data: ScopeDynamicsChartData; kind: 'position' | 'offset' }) {
  const ref = useRef<HTMLDivElement>(null)
  useInlineChart(ref, data, kind)
  return <div ref={ref} className={styles.dynamicsChart} data-chart-kind={kind} />
}

function FactRow({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className={styles.factRow} title={title}>
      <span className={styles.factLabel}>{label}</span>
      <span className={styles.factValue}>{value}</span>
    </div>
  )
}

function CurrentFactStrip({ phaseFact, status }: { phaseFact: ScopePhaseFact | null; status: string }) {
  const f = phaseFact
  return (
    <div className={styles.factStrip}>
      <FactRow label="Status" value={status ? String(status) : NULL_DISPLAY} />
      <FactRow label="Phase" value={formatPhaseLabel(f?.phase)} title="来自末尾 dynamics_phase observation" />
      <FactRow label="Position" value={formatPosition(f?.position)} />
      <FactRow label="Velocity" value={formatNumberNullable(f?.velocity)} />
      <FactRow label="Acceleration" value={formatNumberNullable(f?.acceleration)} />
      <FactRow
        label="Upper Occ"
        value={f?.upper_occupancy === null || f?.upper_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.upper_occupancy)}
      />
      <FactRow
        label="Lower Occ"
        value={f?.lower_occupancy === null || f?.lower_occupancy === undefined ? NULL_DISPLAY : formatPercentNullable(f.lower_occupancy)}
      />
    </div>
  )
}

export default function ScopeDynamicsPanel({ dynamics }: { dynamics: ScopeDynamicsParsed | null }) {
  const positionData = useMemo(() => alignDynamicsSeries(dynamics?.dates ?? [], dynamics?.position ?? []), [dynamics])
  const velocityData = useMemo(() => alignDynamicsSeries(dynamics?.dates ?? [], dynamics?.velocity ?? []), [dynamics])
  const accelerationData = useMemo(
    () => alignDynamicsSeries(dynamics?.dates ?? [], dynamics?.acceleration ?? []),
    [dynamics],
  )
  if (!dynamics) {
    return <div className={styles.panelUnavailable}>该层当前不可用（无 historical_dynamics）</div>
  }
  const current = currentPhaseFact(dynamics)
  return (
    <div className={styles.panel} data-panel="dynamics">
      <SeriesChart data={positionData} kind="position" />
      <SeriesChart data={velocityData} kind="offset" />
      <SeriesChart data={accelerationData} kind="offset" />
      <CurrentFactStrip phaseFact={current} status={dynamics.status} />
    </div>
  )
}

// 保持导出签名稳定，便于契约测试断言（不强制在面板内执行图表库逻辑）
export { alignDynamicsSeries }