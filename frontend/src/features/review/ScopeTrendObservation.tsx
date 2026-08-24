// [R3C] G2/G3/G4 — Trend formal Observation renderer.
// Consumes parsed ViewModels. No recomputation, no strong/weak/confirm/health
// interpretation. Trend direction uses A-share direction colors (up=red, down=green).

import { type FC } from 'react'
import styles from './review.module.scss'
import type {
  TrendStateVM,
  TrendProgressVM,
  TrendVolumeConfirmationVM,
} from './scopePriceTrendContract'

interface TrendProps {
  state?: TrendStateVM | null
  progress?: TrendProgressVM | null
  volume?: TrendVolumeConfirmationVM | null
}

const ScopeTrendObservation: FC<TrendProps> = ({ state, progress, volume }) => {
  return (
    <div className={styles.trendRoot}>
      {state && <TrendStateSection vm={state} />}
      {progress && <TrendProgressSection vm={progress} />}
      {volume && <TrendVolumeSection vm={volume} />}
    </div>
  )
}

// ---- G2: Trend State -------------------------------------------------------

function TrendStateSection({ vm }: { vm: TrendStateVM }) {
  const dir = vm.direction
  return (
    <div className={styles.trendSection}>
      <div className={styles.trendSectionTitle}>趋势状态</div>
      {vm.denominatorZero || !dir ? (
        <div className={styles.trendUnavailable}>无有效趋势状态成员</div>
      ) : (
        <div className={styles.trendDirGrid}>
          <DirBar label="Up" ratio={dir.upRatio} count={dir.upCount} tone="up" />
          <DirBar label="Neutral" ratio={dir.neutralRatio} count={dir.neutralCount} tone="neutral" />
          <DirBar label="Down" ratio={dir.downRatio} count={dir.downCount} tone="down" />
          <div className={styles.trendDenominator}>n = {dir.denominator}</div>
        </div>
      )}
      <div className={styles.trendStateRow}>
        <Analytic label="Trend Strength" value={vm.trendStrength} />
        <Analytic label="DSA-VWAP Dev" value={vm.dsaVwapDevPct} tone={vm.dsaVwapDevTone} />
      </div>
    </div>
  )
}

function DirBar({ label, ratio, count, tone }: { label: string; ratio: number | null; count: number | null; tone: 'up' | 'down' | 'neutral' }) {
  const pct = ratio === null ? 0 : Math.max(0, Math.min(100, ratio * 100))
  return (
    <div className={styles.trendDirCell}>
      <div className={styles.trendDirHead}>
        <span className={styles.trendDirLabel}>{label}</span>
        <span className={`${styles.trendDirPct} ${styles[tone]}`}>{ratio === null ? '—' : `${(ratio * 100).toFixed(0)}%`}</span>
      </div>
      <div className={styles.trendDirTrack}>
        <div className={`${styles.trendDirFill} ${styles[tone]}`} style={{ width: `${pct}%` }} />
      </div>
      <div className={styles.trendDirCount}>{count === null ? '—' : count}</div>
    </div>
  )
}

function Analytic({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: 'up' | 'down' | 'neutral' }) {
  return (
    <div className={styles.trendAnalytic}>
      <div className={styles.trendAnalyticLabel}>{label}</div>
      <div className={`${styles.trendAnalyticValue} ${styles[tone]}`}>{value}</div>
    </div>
  )
}

// ---- G3: Trend Progress ----------------------------------------------------

function TrendProgressSection({ vm }: { vm: TrendProgressVM }) {
  return (
    <div className={styles.trendSection}>
      <div className={styles.trendSectionTitle}>趋势进程</div>
      <div className={styles.trendProgressGrid}>
        <Analytic label="Segment Bars" value={vm.segmentBars} />
        <Analytic label="Segment Change" value={vm.segmentChangePct} tone={vm.segmentChangeTone} />
        <Analytic label="Segment Slope" value={vm.segmentSlope} tone={vm.segmentSlopeTone} />
        <Analytic label="VWAP Return Total" value={vm.vwapRetTotal} tone={vm.vwapRetTotalTone} />
        <Analytic label="Volume Ratio" value={vm.volumeRatio} />
        <Analytic label="Amount Ratio" value={vm.amountRatio} />
      </div>
    </div>
  )
}

// ---- G4: Trend × Volume Confirmation --------------------------------------

function TrendVolumeSection({ vm }: { vm: TrendVolumeConfirmationVM }) {
  const mvr = vm.momentumRelation
  return (
    <div className={styles.trendSection}>
      <div className={styles.trendSectionTitle}>趋势量能确认</div>
      <div className={styles.trendVolumeRatioRow}>
        <Analytic label="Segment Volume Ratio" value={vm.volumeRatio} />
        <Analytic label="Segment Amount Ratio" value={vm.amountRatio} />
      </div>
      <div className={styles.trendMvrBlock}>
        <div className={styles.trendMvrTitle}>Momentum / Volume Relation</div>
        {!mvr || mvr.status === 'unavailable' ? (
          <div className={styles.trendUnavailable}>
            {mvr?.reason ? mvr.reason : 'Momentum / Volume Relation 不可用'}
          </div>
        ) : mvr.categories.length === 0 ? (
          <div className={styles.trendUnavailable}>Momentum / Volume Relation 无有效类别</div>
        ) : (
          <div className={styles.trendMvrList}>
            {mvr.categories.map((c) => (
              <div key={c.category} className={styles.trendMvrRow}>
                <span className={styles.trendMvrCat}>{c.category}</span>
                <span className={`${styles.trendMvrPct} ${styles.neutral}`}>
                  {c.ratio === null ? '—' : `${(c.ratio * 100).toFixed(0)}%`}
                </span>
                <span className={styles.trendMvrCount}>{c.count === null ? '—' : c.count}</span>
              </div>
            ))}
            <div className={styles.trendDenominator}>n = {mvr.denominator}</div>
          </div>
        )}
      </div>
    </div>
  )
}

export default ScopeTrendObservation
