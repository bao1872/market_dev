// [R3C] G1 — Price & Capital formal Observation renderer.
// Consumes parsed PriceCapitalVM. No recomputation, no score/health/bullish.

import { type FC } from 'react'
import styles from './review.module.scss'
import type { PriceCapitalVM, HhiFacts } from './scopePriceTrendContract'

interface Props {
  vm: PriceCapitalVM
}

function HhiPanel({ title, hhi }: { title: string; hhi: HhiFacts | null }) {
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
          <span className={styles.pcHhiKey}>Raw HHI</span>
          <span className={`${styles.pcHhiValue} ${styles.neutral}`}>
            {statusReady ? formatNullableNumberValue(hhi.rawHhi) : '—'}
          </span>
        </div>
        <div>
          <span className={styles.pcHhiKey}>Normalized HHI</span>
          <span className={`${styles.pcHhiValue} ${styles.neutral}`}>
            {statusReady ? formatNullableNumberValue(hhi.normalizedHhi) : '—'}
          </span>
        </div>
        <div>
          <span className={styles.pcHhiKey}>n</span>
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
        <Metric label="Equal-weight Return" value={vm.equalWeightReturn} tone={vm.equalWeightReturnTone} />
        <Metric label="Amount-weighted Return" value={vm.amountWeightedReturn} tone={vm.amountWeightedReturnTone} />
        <Metric label="Total Volume" value={vm.totalVolume} tone="neutral" />
        <Metric label="Total Amount" value={vm.totalAmount} tone="neutral" />
      </div>
      {vm.amountAvailabilityNote && (
        <div className={styles.pcNote}>{vm.amountAvailabilityNote}</div>
      )}
      <div className={styles.pcConcentrationRow}>
        <HhiPanel title="Price Concentration" hhi={vm.priceHhi} />
        <HhiPanel title="Amount Concentration" hhi={vm.amountHhi} />
      </div>
    </div>
  )
}

interface MetricProps {
  label: string
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
