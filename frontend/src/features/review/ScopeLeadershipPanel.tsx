// [ScopeLeadershipPanel] - 描述: Leadership 面板（Slice E correction）
//
// 硬契约（prompt §9）：
// - 数据源 ONLY composition.leadership。
// - Unavailable 侧用 null，绝非 0；empty array 与 null 必须区分。
// - 展示 T-1 leaders → retained/exits/entrants → T leaders 的迁移事实。
// - Metrics：previous retention / jaccard stability / migration（persisted，不重算 1-Jaccard）。
// - status 不可用时展示 canonical reason + 所有非 null 事实，而非完全隐藏。
//   empty_leader_set / unavailable_snapshot 时仍可能保留有效 evidence。
// - 禁止创建 Stable/Rotating/Leadership Strength/Rotation Score/Risk Score 等解释标签。
// - previous_direction / current_direction 为 number | null（+1/-1/null），非 string。
import type { ScopeLeadershipParsed } from './scopeDetailContract'
import {
  NULL_DISPLAY,
  formatNumberNullable,
  formatReadiness,
  displayMember,
  type MemberDirectory,
} from './reviewFormat'
import ReviewTerm from './ReviewTerm'
import styles from './review.module.scss'

function IdList({
  ids,
  label,
  directory,
}: {
  ids: string[] | null
  label: React.ReactNode
  directory: MemberDirectory | null | undefined
}) {
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
              {displayMember(id, directory)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** 方向显示：+1 → ↑, -1 → ↓, null → — */
function directionArrow(dir: number | null): string {
  if (dir === null || dir === undefined) return '—'
  if (dir > 0) return '↑'
  if (dir < 0) return '↓'
  return '—'
}

export default function ScopeLeadershipPanel({
  leadership,
  memberDirectory,
}: {
  leadership: ScopeLeadershipParsed | null
  memberDirectory?: MemberDirectory | null
}) {
  if (!leadership) {
    return <div className={styles.panelUnavailable}>该层当前不可用（无 leadership）</div>
  }

  const status = leadership.status
  const isReady = status === 'ready'
  // 非 ready 时展示 status+reason banner，但仍渲染所有非 null 事实

  return (
    <div className={styles.panel} data-panel="leadership">
      {!isReady && (
        <div className={styles.leadStatusBanner}>
          <span>状态：{formatReadiness(status)}</span>
          {leadership.reason && <span> · 原因：{leadership.reason}</span>}
        </div>
      )}

      <div className={styles.leadRow}>
        <IdList ids={leadership.previousLeaderIds} label={<ReviewTerm termKey="tMinus1Leaders" compact />} directory={memberDirectory} />
        <IdList ids={leadership.currentLeaderIds} label={<ReviewTerm termKey="tLeaders" compact />} directory={memberDirectory} />
      </div>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}><ReviewTerm termKey="direction" compact /></dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="prevDir" compact /></span>
            <span className={styles.metricValue}>{directionArrow(leadership.previousDirection)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="currDir" compact /></span>
            <span className={styles.metricValue}>{directionArrow(leadership.currentDirection)}</span>
          </div>
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}><ReviewTerm termKey="transition" compact /></dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="retained" compact /></span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.retainedCount, 0)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="entrants" compact /></span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.entrantCount, 0)}</span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="exits" compact /></span>
            <span className={styles.metricValue}>{formatNumberNullable(leadership.exitCount, 0)}</span>
          </div>
        </dd>
      </dl>

      <dl className={styles.metricGroup}>
        <dt className={styles.metricHeading}><ReviewTerm termKey="metrics" compact /></dt>
        <dd className={styles.metricGrid}>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="prevRetention" compact /></span>
            <span className={styles.metricValue}>
              {leadership.previousRetention === null || leadership.previousRetention === undefined
                ? NULL_DISPLAY
                : formatNumberNullable(leadership.previousRetention, 3)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="jaccardStability" compact /></span>
            <span className={styles.metricValue}>
              {leadership.jaccardStability === null || leadership.jaccardStability === undefined
                ? NULL_DISPLAY
                : formatNumberNullable(leadership.jaccardStability, 3)}
            </span>
          </div>
          <div className={styles.metricCell}>
            <span className={styles.metricLabel}><ReviewTerm termKey="migration" compact /></span>
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
