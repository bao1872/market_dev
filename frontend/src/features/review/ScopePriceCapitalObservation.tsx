// [R3C] G1 — Price & Capital formal Observation renderer.
// Consumes parsed PriceCapitalVM. No recomputation, no score/health/bullish.
// REVIEW-UX-CN-01：指标 label 中文化 + hover tooltip（ReviewTerm compact，无 ⓘ）。

import { type FC, type ReactNode } from 'react'
import styles from './review.module.scss'
import type { PriceCapitalVM, HhiFacts } from './scopePriceTrendContract'
import ReviewTerm from './ReviewTerm'

interface Props {
  vm: PriceCapitalVM
}

function HhiPanel({ title, hhi }: { title: ReactNode; hhi: HhiFacts | null }) {
  if (!hhi) {
    return (
      <div className={styles.pcHhiBlock}>
        <div className={styles.pcHhiTitle}>{title}</div>
        <div className={`${styles.pcHhiValue} ${styles.neutral}`}>—</div>
      </div>
    )
  }
  const statusReady = hhi.status === 'ready'
  return (
    <div className={styles.pcHhiBlock}>
      <div className={styles.pcHhiTitle}>{title}</div>
      <div className={styles.pcHhiGrid}>
        <div>
          <span className={styles.pcHhiKey}><ReviewTerm termKey="rawHhi" compact /></span>
          <span className={`${styles.pcHhiValue} ${styles.neutral}`}>
            {statusReady ? formatNullableNumberValue(hhi.rawHhi) : '—'}
          </span>
        </div>
        <div>
          <span className={styles.pcHhiKey}><ReviewTerm termKey="normalizedHhi" compact /></span>
          <span className={`${styles.pcHhiValue} ${styles.neutral}`}>
            {statusReady ? formatNullableNumberValue(hhi.normalizedHhi) : '—'}
          </span>
        </div>
        <div>
          <span className={styles.pcHhiKey}><ReviewTerm termKey="sampleCount" compact /></span>
          <span className={`${styles.pcHhiValue} ${styles.neutral}`}>{hhi.memberCount}</span>
        </div>
      </div>
      {!statusReady && <div className={styles.pcHhiStatus}>{hhi.status || 'unavailable'}</div>}
    </div>
  )
}

function formatNullableNumberValue(v: number | null): string {
  if (v === null) return '—'
  return v.toFixed(4)
}

const ScopePriceCapitalObservation: FC<Props> = ({ vm }) => {
  return (
    <div className={styles.pcRoot}>
      <div className={styles.pcRow}>
        <Metric label={<ReviewTerm termKey="equalWeightReturn" compact />} value={vm.equalWeightReturn} tone={vm.equalWeightReturnTone} />
        <Metric label={<ReviewTerm termKey="amountWeightedReturn" compact />} value={vm.amountWeightedReturn} tone={vm.amountWeightedReturnTone} />
        <Metric label={<ReviewTerm termKey="totalVolume" compact />} value={vm.totalVolume} tone="neutral" />
        <Metric label={<ReviewTerm termKey="totalAmount" compact />} value={vm.totalAmount} tone="neutral" />
      </div>
      {vm.amountAvailabilityNote && (
        <div className={styles.pcNote}>{vm.amountAvailabilityNote}</div>
      )}
      <div className={styles.pcConcentrationRow}>
        <HhiPanel title={<ReviewTerm termKey="priceConcentration" compact />} hhi={vm.priceHhi} />
        <HhiPanel title={<ReviewTerm termKey="amountConcentration" compact />} hhi={vm.amountHhi} />
      </div>
    </div>
  )
}

interface MetricProps {
  label: ReactNode
  value: string
  tone: 'up' | 'down' | 'neutral'
}

const Metric: FC<MetricProps> = ({ label, value, tone }) => (
  <div className={styles.pcMetric}>
    <div className={styles.pcMetricLabel}>{label}</div>
    <div className={`${styles.pcMetricValue} ${styles[tone]}`}>{value}</div>
  </div>
)

export default ScopePriceCapitalObservation
