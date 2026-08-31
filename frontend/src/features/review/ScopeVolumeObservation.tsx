// [R3E] G8 — Volume Anomaly formal Observation renderer.
// Consumes parsed VolumeObservationVM. Compact 20D/200D comparison matrix.
// NO denominator (G8 participation distribution has none). valid_count=0 ->
// unavailable display ("—"). Neutral analytical tone; no direction color.

import { type FC } from 'react'
import styles from './review.module.scss'
import {
  type VolumeObservationVM,
  type VolumeDistributionVM,
  formatMultipleNullable,
  formatPercentileNullable,
  formatZScoreNullable,
  NULL_DISPLAY,
} from './scopeMomentumVolumeContract'
import ReviewTerm from './ReviewTerm'
import type { ReviewTermKey } from './reviewCopy'

interface Props {
  vm: VolumeObservationVM
}

type MetricKind = 'ratio' | 'percentile' | 'zscore'

function fmtDist(d: VolumeDistributionVM | null, kind: MetricKind): { primary: string; sub: string } {
  if (!d || d.unavailable) {
    return { primary: NULL_DISPLAY, sub: '—' }
  }
  const fmt =
    kind === 'ratio' ? formatMultipleNullable : kind === 'percentile' ? formatPercentileNullable : formatZScoreNullable
  return {
    primary: fmt(d.p50),
    sub: `${fmt(d.p25)} – ${fmt(d.p75)}`,
  }
}

function MatrixCell({ d, kind }: { d: VolumeDistributionVM | null; kind: MetricKind }) {
  const { primary, sub } = fmtDist(d, kind)
  return (
    <div className={styles.mvMatrixCell}>
      <span className={styles.mvMatrixPrimary}>{primary}</span>
      <span className={styles.mvMatrixSub}>{sub}</span>
      {d && !d.unavailable && (
        <span className={styles.mvMetricSub}>valid={d.validCount ?? NULL_DISPLAY}</span>
      )}
    </div>
  )
}

const ScopeVolumeObservation: FC<Props> = ({ vm }) => {
  const rows: { label: string; termKey: ReviewTermKey; kind: MetricKind; k20: keyof VolumeObservationVM; k200: keyof VolumeObservationVM }[] = [
    { label: '成交量比', termKey: 'volumeRatio', kind: 'ratio', k20: 'ratio20', k200: 'ratio200' },
    { label: '历史分位', termKey: 'percentile', kind: 'percentile', k20: 'percentile20', k200: 'percentile200' },
    { label: 'Z 分数', termKey: 'zScore', kind: 'zscore', k20: 'zscore20', k200: 'zscore200' },
  ]
  return (
    <div className={styles.mvRoot}>
      <div className={styles.mvSection}>
        <div className={styles.mvSectionTitle}>量能异常</div>
        <div className={styles.mvMatrix}>
          <div className={styles.mvMatrixHead} />
          <div className={styles.mvMatrixHead}>20日</div>
          <div className={styles.mvMatrixHead}>200日</div>
          {rows.map((r) => (
            <FragmentRow key={r.label} termKey={r.termKey} kind={r.kind} d20={vm[r.k20]} d200={vm[r.k200]} />
          ))}
        </div>
        <div className={styles.mvNote}>
          成交量比 → ×；历史分位 → 原值（0–100）；Z 分数 → 原始 z。均为中性分析语气，无方向配色。
          200日不可用时显示 “—”（上游 200日就绪条件未满足），不回填 0 或 20日。
        </div>
      </div>
    </div>
  )
}

function FragmentRow({
  termKey,
  kind,
  d20,
  d200,
}: {
  termKey: ReviewTermKey
  kind: MetricKind
  d20: VolumeDistributionVM | null
  d200: VolumeDistributionVM | null
}) {
  return (
    <>
      <div className={styles.mvMatrixRowLabel}>
        <ReviewTerm termKey={termKey} />
      </div>
      <MatrixCell d={d20} kind={kind} />
      <MatrixCell d={d200} kind={kind} />
    </>
  )
}

export default ScopeVolumeObservation
