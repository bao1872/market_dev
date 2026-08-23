// [ScopeSelectedSummary] - 描述: 已选 Scope 摘要面板（Slice D）
// 仅展示 list row 已存在的 facts（scope/name/key、family、readiness、phase、
// position、velocity、acceleration、coverage），明确标注为 selected-scope
// summary 而非 Scope Detail。绝不在 Slice D 调用 getReviewScopeDetail。
import type { ReviewScopeListItem, ReviewScopeFamily } from './types'
import { NULL_DISPLAY, formatPercentNullable, formatNumberNullable, formatPosition } from './reviewFormat'
import styles from './review.module.scss'

const FAMILY_LABEL: Record<ReviewScopeFamily, string> = {
  industry_l1: '一级行业',
  industry_l2: '二级行业',
  industry_l3: '三级行业',
  concept: '概念',
}

const READINESS_LABEL: Record<string, string> = {
  ready: 'ready',
  insufficient_history: 'insufficient_history',
  unavailable_current: 'unavailable_current',
}

export function ScopeSelectedSummary({
  scope,
  family,
}: {
  scope: ReviewScopeListItem | undefined
  family: ReviewScopeFamily
}) {
  if (!scope) {
    return (
      <aside className={styles.selectedSummary}>
        <div className={styles.summaryTitle}>Selected Scope</div>
        <div className={styles.summaryEmpty}>在列表中选择一个 Scope 查看摘要</div>
      </aside>
    )
  }
  const s = scope.summary
  return (
    <aside className={styles.selectedSummary}>
      <div className={styles.summaryHeader}>
        <span className={styles.summaryTitle}>Selected Scope</span>
        <span className={styles.summaryHint}>list summary · 详情见后续</span>
      </div>
      <div className={styles.summaryBody}>
        <div className={styles.summaryName}>{scope.scopeName ?? NULL_DISPLAY}</div>
        <div className={styles.summaryKey}>{scope.scopeKey}</div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Family</span>
          <span className={styles.summaryValue}>{FAMILY_LABEL[family] ?? family}</span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Readiness</span>
          <span className={styles.summaryValue}>
            {READINESS_LABEL[scope.readiness] ?? scope.readiness ?? NULL_DISPLAY}
          </span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Phase</span>
          <span className={styles.summaryValue}>{s?.phase ?? NULL_DISPLAY}</span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Position</span>
          <span className={styles.summaryValue}>{formatPosition(s?.position)}</span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Velocity</span>
          <span className={styles.summaryValue}>{formatNumberNullable(s?.velocity)}</span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Acceleration</span>
          <span className={styles.summaryValue}>{formatNumberNullable(s?.acceleration)}</span>
        </div>
        <div className={styles.summaryField}>
          <span className={styles.summaryLabel}>Coverage</span>
          <span className={styles.summaryValue}>
            {scope.coverageRatio !== null && scope.coverageRatio !== undefined
              ? formatPercentNullable(scope.coverageRatio)
              : NULL_DISPLAY}
          </span>
        </div>
      </div>
    </aside>
  )
}