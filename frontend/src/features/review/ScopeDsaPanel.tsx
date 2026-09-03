// [ScopeDsaPanel] - 描述: R3 Slice 1 DSA 趋势与结构研究页。
//
// 仅承载后端三类数据，前端不重算：
// - current：observation.trend.continuous（regime_strength / dsa_vwap_dev_pct / segment）
//   + trend.state（up/down/range 成员比）+ trend.transition（T-1→T 迁移）。
// - history：后端 20D 滚动诊断（series / mean20 / std20 / zscore20 / percentile20）。
// - crossSection：C1 empirical percentile（published-run lineage cohort）。
//
// 图表用内联 SVG sparkline（不引图表库）；缺失值 = null，绝不显示 0。
import type { ReviewCrossSectionDTO, ReviewScopeHistoryDTO } from './types'
import styles from './review.module.scss'

type Json = Record<string, unknown>

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function deepGet(root: unknown, path: string[]): unknown {
  let node: unknown = root
  for (const key of path) {
    if (node && typeof node === 'object' && key in (node as Json)) {
      node = (node as Json)[key]
    } else {
      return null
    }
  }
  return node
}

function fmtPct(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}
function fmtNum(v: number | null, digits = 2): string {
  return v == null ? '—' : v.toFixed(digits)
}

/** 内联 sparkline：series 折线 + 可选 mean±std 参考带。 */
function Sparkline({
  series,
  mean,
  std,
  width = 240,
  height = 48,
}: {
  series: Array<number | null>
  mean?: Array<number | null>
  std?: Array<number | null>
  width?: number
  height?: number
}) {
  const vals = series.filter((v): v is number => v != null)
  if (vals.length === 0) {
    return <span className={styles.kvVal}>—</span>
  }
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const pad = 4
  const n = series.length
  const x = (i: number) => pad + (i / Math.max(1, n - 1)) * (width - 2 * pad)
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - 2 * pad)
  const pts = series
    .map((v, i) => (v == null ? '' : `${x(i).toFixed(1)},${y(v).toFixed(1)}`))
    .filter(Boolean)
    .join(' ')
  // mean±std 参考带（取末点）
  const lastMean = mean ? mean[mean.length - 1] : null
  const lastStd = std ? std[std.length - 1] : null
  let band: string | null = null
  if (lastMean != null && lastStd != null && Number.isFinite(lastStd)) {
    const yTop = y(lastMean + lastStd)
    const yBot = y(lastMean - lastStd)
    band = `${pad},${yTop.toFixed(1)} ${width - pad},${yTop.toFixed(1)} ${width - pad},${yBot.toFixed(1)} ${pad},${yBot.toFixed(1)}`
  }
  return (
    <svg width={width} height={height} role="img" aria-label="20日序列" style={{ display: 'block' }}>
      {band && <polygon points={band} fill="rgba(120,160,255,0.14)" />}
      <polyline points={pts} fill="none" stroke="#4f8cff" strokeWidth={1.5} />
    </svg>
  )
}

function HistoryRow({ f }: { f: ReviewScopeHistoryDTO['fields'][string] }) {
  const i = f.series.length - 1
  const cur = f.series[i] ?? null
  const z = f.zscore20[i] ?? null
  const p = f.percentile20[i] ?? null
  return (
    <div className={styles.kvRow}>
      <span className={styles.kvKey}>{f.label}</span>
      <Sparkline series={f.series} mean={f.mean20} std={f.std20} />
      <span className={styles.kvVal}>
        值 {fmtNum(cur)} · Z {fmtNum(z)} · 分位 {p == null ? '—' : p.toFixed(0)}
      </span>
    </div>
  )
}

export interface ScopeDsaPanelProps {
  observation: Json | null
  history: ReviewScopeHistoryDTO | null
  crossSection: ReviewCrossSectionDTO | null
}

export default function ScopeDsaPanel({ observation, history, crossSection }: ScopeDsaPanelProps) {
  const continuous = deepGet(observation, ['trend', 'continuous']) as Json | null
  const trendState = deepGet(observation, ['trend', 'state']) as Json | null
  const transition = deepGet(observation, ['trend', 'transition']) as Json | null

  const regime = num(deepGet(continuous, ['regime_strength']))
  const vwapDev = num(deepGet(continuous, ['dsa_vwap_dev_pct']))
  const segBars = num(deepGet(continuous, ['segment_bars']))
  const segSlope = num(deepGet(continuous, ['segment_slope']))
  const segChange = num(deepGet(continuous, ['segment_change_pct']))

  const upRatio = num(deepGet(trendState, ['up_ratio']))
  const downRatio = num(deepGet(trendState, ['down_ratio']))
  const rangeRatio = num(deepGet(trendState, ['range_ratio']))

  const csRegime = crossSection?.fields.find((f) => f.field_key === 'trend.continuous.regime_strength')

  const histFields = history?.fields ?? {}

  return (
    <div className={styles.dsaPanel}>
      {/* current */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>当前 DSA 趋势与结构</div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>Regime Strength</span>
          <span className={styles.kvVal}>{fmtNum(regime)}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势段 VWAP 偏离</span>
          <span className={styles.kvVal}>{fmtPct(vwapDev)}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势段</span>
          <span className={styles.kvVal}>
            {segBars == null ? '—' : `${segBars.toFixed(0)} 根`}
            {segSlope != null ? ` · 斜率 ${fmtNum(segSlope)}` : ''}
            {segChange != null ? ` · 涨跌 ${fmtPct(segChange)}` : ''}
          </span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>成员构成</span>
          <span className={styles.kvVal}>
            涨 {fmtPct(upRatio)} · 跌 {fmtPct(downRatio)} · 横 {fmtPct(rangeRatio)}
          </span>
        </div>
        {transition && (
          <div className={styles.kvRow}>
            <span className={styles.kvKey}>T-1→T 迁移</span>
            <span className={styles.kvVal}>{JSON.stringify(transition)}</span>
          </div>
        )}
      </section>

      {/* crossSection */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>横截面位置（C1 empirical percentile）</div>
        {csRegime ? (
          <div className={styles.kvRow}>
            <span className={styles.kvKey}>Regime Strength 分位</span>
            <span className={styles.kvVal}>
              {csRegime.percentile == null ? '样本不足' : `${csRegime.percentile.toFixed(0)} 分位`}
              {' · '}peer {csRegime.peer_count}
            </span>
          </div>
        ) : (
          <div className={styles.kvVal}>无横截面证据（market scope 或非 activated）</div>
        )}
      </section>

      {/* history */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>20 日滚动诊断（published-run lineage）</div>
        {history && history.availability.status !== 'empty' ? (
          <>
            {histFields.regime_strength && <HistoryRow f={histFields.regime_strength} />}
            {histFields.dsa_vwap_dev_pct && <HistoryRow f={histFields.dsa_vwap_dev_pct} />}
            {histFields.trend_up_ratio && <HistoryRow f={histFields.trend_up_ratio} />}
            {histFields.trend_down_ratio && <HistoryRow f={histFields.trend_down_ratio} />}
            {histFields.trend_range_ratio && <HistoryRow f={histFields.trend_range_ratio} />}
          </>
        ) : (
          <div className={styles.kvVal}>非 activated scope（market/major_index/style）无 20 日历史</div>
        )}
      </section>
    </div>
  )
}
