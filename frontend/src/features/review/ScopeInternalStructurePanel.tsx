// [ScopeInternalStructurePanel] - 描述: Internal Structure 面板（Slice E）
//
// 硬契约（prompt §7）：
// - 数据源 ONLY composition.internal_structure_facts。
// - Breadth：advance/decline/unchanged 100% 横向堆叠，不合成 composite score。
// - Return/Capital：EW Return / AW Return / persisted Capital Tilt（绝不前端计算 AW-EW）。
// - A-share direction colors ONLY 用于市场方向事实：positive = red、negative = green、zero = neutral。
// - HHI 是中性分析信息，绝不上色（不高 HHI=风险 / 低 HHI=好）。
// - 禁止文本：资金认可/资金背离/结构健康/风险/机会/强/弱。
import type { ScopeInternalParsed } from './scopeDetailContract'
import { NULL_DISPLAY, formatPercentNullable, formatNumberNullable } from './reviewFormat'
import styles from './review.module.scss'

function directionClass(value: number | null, safeNeutral = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ''
  if (value > 0) return styles.up
  if (value < 0) return styles.down
  return safeNeutral ? '' : ''
}

function BreadthStack({ breadth }: { breadth: ScopeInternalParsed['breadth'] }) {
  if (!breadth) return <div className={styles.panelUnavailable}>Breadth 不可用</div>
  const a = breadth.advanceRatio ?? 0
  const d = breadth.declineRatio ?? 0
  const u = breadth.unchangedRatio ?? 0
  const total = a + d + u
  return (
    <dl className={styles.metricGroup}>
      <dt className={styles.metricHeading}>Breadth</dt>
      <dd className={styles.breadthStack}>
        <div
          className={`${styles.breadthSegment} ${styles.up}`}
          style={{ width: total > 0 ? `${(a / total) * 100}%` : '0%' }}
          title={`advance <span>${breadth.advanceRatio ?? NULL_DISPLAY}</span>`}
        />
        <div
          className={`${styles.breadthSegment} ${styles.down}`}
          style={{ width: total > 0 ? `${(d / total) * 100}%` : '0%' }}
          title={`decline <span>${breadth.declineRatio ?? NULL_DISPLAY}</span>`}
        />
        <div
          className={`${styles.breadthSegment} ${styles.breadthNeutral}`}
          style={{ width: total > 0 ? `${(u / total) * 100}%` : '0%' }}
          title={`unchanged <span>${breadth.unchangedRatio ?? NULL_DISPLAY}</span>`}
        />
      </dd>
      <div className={styles.breadthLegend}>
        <span className={`${styles.legendDot} ${styles.up}`} /> advance
        <span className={`${styles.legendDot} ${styles.down}`} /> decline
        <span className={`${styles.legendDot} ${styles.breadthNeutral}`} /> unchanged
      </div>
    </dl>
  )
}

export default function ScopeInternalStructurePanel({
  internal,
}: {
  internal: ScopeInternalParsed
}) {
  const ct = internal.capitalTilt
  const con = internal.concentration
  const breadth = internal.breadth
  return (
    <div className={styles.panel} data-panel="internal">
      <BreadthStack breadth={breadth} />

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Return / Capital</dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>EW Return</span>
            <span className={`${styles.metricValue} ${directionClass(ct?.equalWeightReturn ?? null)}`}>
              {formatPercentNullable(ct?.equalWeightReturn, 2)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>AW Return</span>
            <span className={`${styles.metricValue} ${directionClass(ct?.amountWeightedReturn ?? null)}`}>
              {formatPercentNullable(ct?.amountWeightedReturn, 2)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Capital Tilt</span>
            <span className={`${styles.metricValue} ${directionClass(ct?.capitalTilt ?? null)}`}>
              {formatNumberNullable(ct?.capitalTilt, 3)}
            </span>
          </div>
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Concentration</dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Price HHI</span>
            <span className={styles.metricValue}>{formatNumberNullable(con?.priceNormalizedHhi, 3)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Amount HHI</span>
            <span className={styles.metricValue}>{formatNumberNullable(con?.amountNormalizedHhi, 3)}</span>
          </div>
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Return Dispersion</dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Dispersion</span>
            <span className={styles.metricValue}>{formatNumberNullable(breadth?.returnDispersion, 3)}</span>
          </div>
        </dd>
      </dl>
    </div>
  )
}

export { directionClass }