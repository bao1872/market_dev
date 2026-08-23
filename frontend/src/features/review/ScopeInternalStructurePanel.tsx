// [ScopeInternalStructurePanel] - 描述: Internal Structure 面板（Slice E correction）
//
// 硬契约（prompt §5、§8）：
// - 数据源 ONLY composition.internal_structure_facts。
// - Breadth：advance/decline/unchanged 使用 persisted ratio，不重算、不伪造 0。
// - Return/Capital：EW Return / AW Return / persisted Capital Tilt（绝不前端计算 AW-EW）。
// - A-share direction colors ONLY 用于市场方向事实：positive = red、negative = green、zero = neutral。
// - HHI 是中性分析信息，绝不上色（不高 HHI=风险 / 低 HHI=好）。
// - 禁止文本：资金认可/资金背离/结构健康/风险/机会/强/弱。
// - null != 0：缺失 ratio 时展示 unavailable，不画假 100% 堆叠条。
import type { ScopeInternalParsed } from './scopeDetailContract'
import { NULL_DISPLAY, formatPercentNullable, formatNumberNullable } from './reviewFormat'
import styles from './review.module.scss'

function directionClass(value: number | null, safeNeutral = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ''
  if (value > 0) return styles.up
  if (value < 0) return styles.down
  return safeNeutral ? '' : ''
}

/**
 * 宽度百分比：使用 persisted ratio * 100，不重新归一化。
 * null 时返回 null（调用方判断是否画堆叠条）。
 */
function widthPercent(ratio: number | null | undefined): number | null {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return null
  return ratio * 100
}

function BreadthStack({ breadth }: { breadth: ScopeInternalParsed['breadth'] }) {
  if (!breadth) return <div className={styles.panelUnavailable}>Breadth 不可用</div>
  const a = widthPercent(breadth.advanceRatio)
  const d = widthPercent(breadth.declineRatio)
  const u = widthPercent(breadth.unchangedRatio)

  // 三者全部非 null → 使用 persisted ratio 宽度
  // 任一 null → 不画假 100% 堆叠，展示 partial/unavailable 状态
  const allReady = a !== null && d !== null && u !== null

  return (
    <dl className={styles.metricGroup}>
      <dt className={styles.metricHeading}>Breadth</dt>
      <dd className={styles.breadthStack}>
        {allReady ? (
          <>
            <div
              className={`${styles.breadthSegment} ${styles.up}`}
              style={{ width: `${a}%` }}
              title={`advance ${breadth.advanceRatio ?? NULL_DISPLAY}`}
            />
            <div
              className={`${styles.breadthSegment} ${styles.down}`}
              style={{ width: `${d}%` }}
              title={`decline ${breadth.declineRatio ?? NULL_DISPLAY}`}
            />
            <div
              className={`${styles.breadthSegment} ${styles.breadthNeutral}`}
              style={{ width: `${u}%` }}
              title={`unchanged ${breadth.unchangedRatio ?? NULL_DISPLAY}`}
            />
          </>
        ) : (
          <div className={styles.breadthPartial}>
            <div className={styles.breadthRow}>
              <span className={`${styles.legendDot} ${styles.up}`} />
              <span>advance</span>
              <span className={styles.breadthValue}>{breadth.advanceRatio === null || breadth.advanceRatio === undefined ? NULL_DISPLAY : formatPercentNullable(breadth.advanceRatio, 1)}</span>
            </div>
            <div className={styles.breadthRow}>
              <span className={`${styles.legendDot} ${styles.down}`} />
              <span>decline</span>
              <span className={styles.breadthValue}>{breadth.declineRatio === null || breadth.declineRatio === undefined ? NULL_DISPLAY : formatPercentNullable(breadth.declineRatio, 1)}</span>
            </div>
            <div className={styles.breadthRow}>
              <span className={`${styles.legendDot} ${styles.breadthNeutral}`} />
              <span>unchanged</span>
              <span className={styles.breadthValue}>{breadth.unchangedRatio === null || breadth.unchangedRatio === undefined ? NULL_DISPLAY : formatPercentNullable(breadth.unchangedRatio, 1)}</span>
            </div>
          </div>
        )}
      </dd>
      {allReady && (
        <div className={styles.breadthLegend}>
          <span className={`${styles.legendDot} ${styles.up}`} /> advance
          <span className={`${styles.legendDot} ${styles.down}`} /> decline
          <span className={`${styles.legendDot} ${styles.breadthNeutral}`} /> unchanged
        </div>
      )}
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
