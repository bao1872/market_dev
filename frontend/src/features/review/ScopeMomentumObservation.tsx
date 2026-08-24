// [R3E] G7 — Momentum formal Observation renderer.
// Consumes parsed MomentumObservationVM. No recomputation, no score/signal,
// no direction color. squeeze denominator=0 -> unavailable (NOT 0%).

import { type FC } from 'react'
import styles from './review.module.scss'
import {
  type MomentumObservationVM,
  type CurrentOnlyDistributionVM,
  type SqueezeStateVM,
  fmtSqueezeRatio,
  fmtSqueezeCategory,
  formatRawDimensionlessNullable,
  formatMultipleNullable,
  NULL_DISPLAY,
} from './scopeMomentumVolumeContract'

interface Props {
  vm: MomentumObservationVM
}

function SqueezeStateBlock({ squeeze }: { squeeze: SqueezeStateVM | null }) {
  if (!squeeze) {
    return <div className={styles.mvNeutral}>暂无 Squeeze 状态分布</div>
  }
  if (squeeze.unavailable) {
    return <div className={styles.mvUnavailable}>无有效成员（denominator=0）→ 全部占比不可用，非 0%</div>
  }
  return (
    <div className={styles.mvSqueezeGrid}>
      {squeeze.categories.map((c) => (
        <div key={c.category} className={styles.mvSqueezeRow}>
          <span className={styles.mvSqueezeCat}>{fmtSqueezeCategory(c.category)}</span>
          <span className={styles.mvSqueezeRatio}>{fmtSqueezeRatio(c.ratio)}</span>
          <span className={styles.mvSqueezeCount}>n={c.count}</span>
        </div>
      ))}
      <div className={styles.mvDenominator}>denominator (有效成员) = {squeeze.denominator}</div>
    </div>
  )
}

function CurrentOnlyBlock({
  title,
  dist,
  unit,
}: {
  title: string
  dist: CurrentOnlyDistributionVM | null
  unit: 'raw' | 'multiple'
}) {
  const fmt = unit === 'multiple' ? formatMultipleNullable : formatRawDimensionlessNullable
  if (!dist) {
    return (
      <div className={styles.mvBlock}>
        <div className={styles.mvBlockTitle}>{title}</div>
        <div className={styles.mvNeutral}>暂无事实</div>
      </div>
    )
  }
  if (dist.unavailable) {
    return (
      <div className={styles.mvBlock}>
        <div className={styles.mvBlockTitle}>{title}</div>
        <div className={styles.mvUnavailable}>
          不可用{dist.reason ? `：${dist.reason}` : ''}（valid_count=0）
        </div>
      </div>
    )
  }
  return (
    <div className={styles.mvBlock}>
      <div className={styles.mvBlockTitle}>{title}</div>
      <div className={styles.mvMetricRow}>
        <div className={styles.mvMetric}>
          <span className={styles.mvMetricLabel}>Median</span>
          <span className={styles.mvMetricValue}>{fmt(dist.median)}</span>
        </div>
        <div className={styles.mvMetric}>
          <span className={styles.mvMetricLabel}>P25–P75</span>
          <span className={styles.mvMetricValue}>
            {fmt(dist.p25)} – {fmt(dist.p75)}
          </span>
        </div>
        <div className={styles.mvMetric}>
          <span className={styles.mvMetricLabel}>valid</span>
          <span className={styles.mvMetricValue}>{dist.validCount ?? NULL_DISPLAY}</span>
        </div>
        <div className={styles.mvMetric}>
          <span className={styles.mvMetricLabel}>denominator</span>
          <span className={styles.mvMetricValue}>{dist.denominator ?? NULL_DISPLAY}</span>
        </div>
      </div>
    </div>
  )
}

const ScopeMomentumObservation: FC<Props> = ({ vm }) => {
  return (
    <div className={styles.mvRoot}>
      <div className={styles.mvSection}>
        <div className={styles.mvSectionTitle}>Squeeze State</div>
        <SqueezeStateBlock squeeze={vm.squeeze} />
      </div>
      <div className={styles.mvSection}>
        <div className={styles.mvSectionTitle}>BB Position</div>
        <CurrentOnlyBlock title="" dist={vm.bbPosition} unit="raw" />
        <div className={styles.mvNote}>0 = Lower Band / 1 = Upper Band；band 外值合法，不做 clamp</div>
      </div>
      <div className={styles.mvSection}>
        <div className={styles.mvSectionTitle}>BB Width</div>
        <CurrentOnlyBlock title="" dist={vm.bbWidth} unit="raw" />
        <div className={styles.mvNote}>无量纲宽度比率，非百分比（不 ×100）</div>
      </div>
      <div className={styles.mvSection}>
        <div className={styles.mvSectionTitle}>Release Volume Ratio</div>
        <CurrentOnlyBlock title="" dist={vm.releaseVolumeRatio} unit="multiple" />
        <div className={styles.mvNote}>无量纲倍数（×），无方向配色</div>
      </div>
    </div>
  )
}

export default ScopeMomentumObservation
