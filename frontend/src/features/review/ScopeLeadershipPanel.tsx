// [ScopeLeadershipPanel] - 描述: Leadership 面板（Slice E）
//
// 硬契约（prompt §8）：
// - 数据源 ONLY composition.leadership。
// - Unavailable 侧用 null，绝非 0；empty array 与 null 必须区分。
// - 展示 T-1 leaders → retained/exits/entrants → T leaders 的迁移事实。
// - Metrics：previous retention / jaccard stability / migration（persisted，不重算 1-Jaccard）。
// - status 不可用时展示 canonical reason。
// - 禁止创建 Stable/Rotating/Leadership Strength/Rotation Score/Risk Score 等解释标签。
import type { ScopeLeadershipParsed } from './scopeDetailContract'
import { NULL_DISPLAY, formatNumberNullable } from './reviewFormat'
import styles from './review.module.scss'

function IdList({ ids, label }: { ids: string[] | null; label: string }) {
  const isEmpty = ids !== null && ids.length === 0
  return (
    <div className={styles.leadGroup}>
      <div className={styles.leadGroupLabel}>{label}</div>
      {isEmpty ? (
        <div className={styles.leadIdsEmpty}>（空）</div>
      ) : ids === null ? (
        <div className={styles.leadIds}>—</div>
      ) : (
        <div className={styles.leadIds}>
          {ids.map((id) => (
            <span key={id} className={styles.leadChip} title={id}>
              {id}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ScopeLeadershipPanel({
  leadership,
}: {
  leadership: ScopeLeadershipParsed | null
}) {
  if (!leadership) {
    return <div className={styles.panelUnavailable}>该层当前不可用（无 leadership）</div>
  }
  if (leadership.status && String(leadership.status) !== 'ready') {
    return (
      <div className={styles.panelUnavailable}>
        该层状态：{String(leadership.status)}
        {leadership.reason ? ` — ${leadership.reason}` : ''}
      </div>
    )
  }
  return (
    <div className={styles.panel} data-panel="leadership">
      <div className={styles.leadRow}>
        <IdList ids={leadership.previousLeaderIds} label="T-1 leaders" />
        <IdList ids={leadership.currentLeaderIds} label="T leaders" />
      </div>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Transition</dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Retained</span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.retainedCount, 0)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Entrants</span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.entrantCount, 0)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Exits</span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.exitCount, 0)}</span>
          </div>
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}>Metrics</dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Prev Retention</span>
            <span className={styles.metricValue}>
              {leadership.previousRetention === null || leadership.previousRetention === undefined
                ? NULL_DISPLAY
                : formatNumberNullable(leadership.previousRetention, 3)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Jaccard Stability</span>
            <span className={styles.metricValue}>
              {leadership.jaccardStability === null || leadership.jaccardStability === undefined
                ? NULL_DISPLAY
                : formatNumberNullable(leadership.jaccardStability, 3)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}>Migration</span>
            <span className={styles.metricValue}>
              {leadership.migration === null || leadership.migration === undefined
                ? NULL_DISPLAY
                : formatNumberNullable(leadership.migration, 3)}
            </span>
          </div>
        </dd>
      </dl>
    </div>
  )
}