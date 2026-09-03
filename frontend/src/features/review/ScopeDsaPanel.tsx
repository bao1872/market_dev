// [ScopeDsaPanel] - R3 Slice 1 DSA 趋势与结构研究页。
//
// 仅承载三类数据，前端不重算：
// - current：由 scopeDsaContract 解析（复用 R3C 数值刻度 owner，绝不 x100）。
// - history：后端 20D 滚动诊断（series / mean20 / std20 / zscore20 / percentile20）。
// - crossSection：C1 empirical percentile（published-run lineage cohort）。
//
// 图表用内联 SVG sparkline；缺失值 = null，遇 null 断开分段（绝不跨 null 连线）。
// 组件不再 deepGet canonical payload，也不再自建单位 formatter。
import type { ReviewCrossSectionDTO, ReviewScopeHistoryDTO } from './types'
import {
  parseDsaObservation,
  buildDsaVM,
  splitSeriesByGap,
} from './scopeDsaContract'
import { displayMember, type MemberDirectory } from './reviewFormat'
import styles from './review.module.scss'

type Json = Record<string, unknown>

/** 内联 sparkline：遇 null 断开为多个 segment，绝不跨缺失槽连线（P1-3）。 */
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
  const segments = splitSeriesByGap(series)
  const allVals = segments.flatMap((seg) => seg.map((p) => p.v))
  if (allVals.length === 0) {
    return <span className={styles.kvVal}>—</span>
  }
  const min = Math.min(...allVals)
  const max = Math.max(...allVals)
  const span = max - min || 1
  const pad = 4
  const n = series.length
  const x = (i: number) => pad + (i / Math.max(1, n - 1)) * (width - 2 * pad)
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - 2 * pad)
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
      {segments.map((seg, idx) => {
        const pts = seg
          .map((p) => `${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`)
          .join(' ')
        return (
          <polyline
            key={idx}
            points={pts}
            fill="none"
            stroke="#4f8cff"
            strokeWidth={1.5}
          />
        )
      })}
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
        值 {cur == null ? '—' : cur.toFixed(2)} · Z {z == null ? '—' : z.toFixed(1)} · 分位 {p == null ? '—' : p.toFixed(0)}
      </span>
    </div>
  )
}

export interface ScopeDsaPanelProps {
  observation: Json | null
  history: ReviewScopeHistoryDTO | null
  crossSection: ReviewCrossSectionDTO | null
  memberDirectory?: MemberDirectory | null
}

export default function ScopeDsaPanel({ observation, history, crossSection, memberDirectory }: ScopeDsaPanelProps) {
  const vm = buildDsaVM(parseDsaObservation(observation))
  const csRegime = crossSection?.fields.find((f) => f.field_key === 'trend.continuous.regime_strength')

  const histFields = history?.fields ?? {}

  return (
    <div className={styles.dsaPanel}>
      {/* current */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>当前 DSA 趋势与结构</div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>Regime Strength</span>
          <span className={styles.kvVal}>{vm.regimeStrength}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势段 VWAP 偏离</span>
          <span className={styles.kvVal}>{vm.dsaVwapDevPct}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势段</span>
          <span className={styles.kvVal}>
            {vm.segmentBars} 根
            {vm.segmentSlope !== '—' ? ` · 斜率 ${vm.segmentSlope}` : ''}
            {vm.segmentChangePct !== '—' ? ` · 涨跌 ${vm.segmentChangePct}` : ''}
          </span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>成员构成</span>
          <span className={styles.kvVal}>
            涨 {vm.upRatio} · 横 {vm.neutralRatio} · 跌 {vm.downRatio}
          </span>
        </div>
      </section>

      {/* 当前横截面分布（canonical：trend_strength_distribution / dsa_vwap_dev_pct_distribution / dsa_dir_bars_distribution） */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>当前分布（成员级 percentile 描述）</div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势强度分布</span>
          <span className={styles.kvVal}>{vm.trendStrengthDist}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>DSA VWAP 偏离分布</span>
          <span className={styles.kvVal}>{vm.dsaVwapDevDist}</span>
        </div>
        <div className={styles.kvRow}>
          <span className={styles.kvKey}>趋势持续时间分布</span>
          <span className={styles.kvVal}>{vm.dsaDirBarsDist}</span>
        </div>
        {vm.dsaDirBarsBuckets.length > 0 && (
          <div className={styles.bucketRow}>
            {vm.dsaDirBarsBuckets.map((b) => (
              <span key={b.label} className={styles.bucketChip} title={`${b.label} bars`}>
                {b.label}：{b.count}（{b.ratio}）
              </span>
            ))}
          </div>
        )}
      </section>

      {/* T-1 → T 变化（canonical transition：先展示变化成员，ratio 为辅助） */}
      <section className={styles.detailCard}>
        <div className={styles.panelTitle}>T-1 → T 变化成员</div>
        {vm.transitionDenominator == null || vm.transitionDenominator === 0 ? (
          <div className={styles.kvVal}>迁移数据不可用（T-1/T 共同有效成员不足）</div>
        ) : vm.changedMembers.length > 0 ? (
          <div className={styles.changedList}>
            {vm.changedMembers.map((m) => (
              <div key={m.memberId} className={styles.kvRow}>
                <span className={styles.kvKey}>{displayMember(m.memberId, memberDirectory)}</span>
                <span className={styles.kvVal}>
                  {m.previousState} → {m.currentState}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className={styles.kvVal}>无成员发生状态变化（成员状态稳定）</div>
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
