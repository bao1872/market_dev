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
import ReviewTerm from './ReviewTerm'
import type { ReviewTermKey } from './reviewCopy'

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
          <DirBar label="上涨" ratio={dir.upRatio} count={dir.upCount} tone="up" />
          <DirBar label="平盘" ratio={dir.neutralRatio} count={dir.neutralCount} tone="neutral" />
          <DirBar label="下跌" ratio={dir.downRatio} count={dir.downCount} tone="down" />
          <div className={styles.trendDenominator}>n = {dir.denominator}</div>
        </div>
      )}
      <div className={styles.trendStateRow}>
        <Analytic termKey="trendStrength" label="趋势强度" value={vm.trendStrength} />
        <Analytic termKey="dsaVwapDev" label="均价偏离" value={vm.dsaVwapDevPct} tone={vm.dsaVwapDevTone} />
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

// P0-4/P0-5：label 经 ReviewTerm 统一中文 + tooltip（单一展示 owner）。
function Analytic({ label, value, termKey, tone = 'neutral' }: { label: string; value: string; termKey?: ReviewTermKey; tone?: 'up' | 'down' | 'neutral' }) {
  return (
    <div className={styles.trendAnalytic}>
      <div className={styles.trendAnalyticLabel}>
        {termKey ? <ReviewTerm termKey={termKey} /> : label}
      </div>
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
        <Analytic termKey="segmentBars" label="持续K数" value={vm.segmentBars} />
        <Analytic termKey="segmentChange" label="区间涨跌" value={vm.segmentChangePct} tone={vm.segmentChangeTone} />
        <Analytic termKey="segmentSlope" label="趋势斜率" value={vm.segmentSlope} tone={vm.segmentSlopeTone} />
        <Analytic termKey="vwapRetTotal" label="均价累计收益" value={vm.vwapRetTotal} tone={vm.vwapRetTotalTone} />
        <Analytic termKey="volumeRatio" label="量比" value={vm.volumeRatio} />
        <Analytic termKey="amountRatio" label="额比" value={vm.amountRatio} />
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
        <Analytic termKey="volumeRatio" label="分段量比" value={vm.volumeRatio} />
        <Analytic termKey="amountRatio" label="分段额比" value={vm.amountRatio} />
      </div>
      <div className={styles.trendMvrBlock}>
        <div className={styles.trendMvrTitle}>动量与量能关系</div>
        {!mvr || mvr.status === 'unavailable' ? (
          <div className={styles.trendUnavailable}>
            {mvr?.reason ? mvr.reason : '动量与量能关系不可用'}
          </div>
        ) : mvr.categories.length === 0 ? (
          <div className={styles.trendUnavailable}>动量与量能关系无有效类别</div>
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
