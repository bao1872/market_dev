// [ScopeExplorerToolbar] - 描述: Scope Explorer 顶部工具栏（Slice D + R2A）
// 仅展示已激活 canonical family（industry_l1/l2/l3/concept）；
// 不暴露 market/major_index/style；无“全部类型”模式。
// q / phase / readiness / sort 均为展示层状态（sort 为 R2A 新增的单一排序 owner）。
// REVIEW-UX-CN-01：所有用户可见 label 中文化；canonical value（phase/readiness/sort）不变。
import type { ReviewScopeFamily, ReviewDynamicsPhase, ReviewCompositionReadiness } from './types'
import type { ReviewExplorerView, ReviewUrlState, ReviewSort } from './urlState'
import { PHASE_LABELS, READINESS_LABELS, SORT_LABELS } from './reviewCopy'
import styles from './review.module.scss'

const FAMILY_OPTIONS: ReadonlyArray<{ value: ReviewScopeFamily; label: string }> = [
  { value: 'industry_l1', label: '一级行业' },
  { value: 'industry_l2', label: '二级行业' },
  { value: 'industry_l3', label: '三级行业' },
  { value: 'concept', label: '概念' },
]

const PHASE_OPTIONS: ReadonlyArray<ReviewDynamicsPhase> = [
  'Early Lift',
  'Strengthening',
  'Sustained',
  'Decelerating',
  'Weakening',
  'Repairing',
]

const READINESS_OPTIONS: ReadonlyArray<ReviewCompositionReadiness> = [
  'ready',
  'insufficient_history',
  'unavailable_current',
]

// R2A/R2B：排序枚举 → 用户可见 label（canonical value 不变，中文经 reviewCopy.SORT_LABELS）
const SORT_OPTIONS: ReadonlyArray<{ value: ReviewSort; label: string }> = [
  { value: 'velocity_desc', label: SORT_LABELS.velocity_desc },
  { value: 'acceleration_desc', label: SORT_LABELS.acceleration_desc },
  { value: 'position_desc', label: SORT_LABELS.position_desc },
  { value: 'equal_weight_return_desc', label: SORT_LABELS.equal_weight_return_desc },
  { value: 'capital_tilt_desc', label: SORT_LABELS.capital_tilt_desc },
  { value: 'migration_desc', label: SORT_LABELS.migration_desc },
  { value: 'coverage_desc', label: SORT_LABELS.coverage_desc },
  { value: 'freshness_density_desc', label: SORT_LABELS.freshness_density_desc },
  { value: 'freshness_today_desc', label: SORT_LABELS.freshness_today_desc },
  { value: 'technical_hhi_desc', label: SORT_LABELS.technical_hhi_desc },
  { value: 'leader_median_gap_desc', label: SORT_LABELS.leader_median_gap_desc },
]

export interface ScopeExplorerToolbarProps {
  family: ReviewScopeFamily
  view: ReviewExplorerView
  q: string
  phase: ReviewDynamicsPhase | null
  readiness: ReviewCompositionReadiness | null
  sort: ReviewSort
  onFamilyChange: (family: ReviewScopeFamily) => void
  onViewChange: (view: ReviewExplorerView) => void
  onFilterChange: (patch: Partial<ReviewUrlState>) => void
}

export default function ScopeExplorerToolbar({
  family,
  view,
  q,
  phase,
  readiness,
  sort,
  onFamilyChange,
  onViewChange,
  onFilterChange,
}: ScopeExplorerToolbarProps) {
  return (
    <div className={styles.explorerToolbar}>
      <div className={styles.familyTabs} role="tablist" aria-label="Scope 族">
        {FAMILY_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={family === opt.value}
            className={family === opt.value ? `${styles.familyTab} ${styles.familyTabActive}` : styles.familyTab}
            onClick={() => onFamilyChange(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>

      <div className={styles.toolbarRow}>
        <input
          className={styles.searchInput}
          type="search"
          placeholder="搜索板块 / 概念名称"
          value={q}
          onChange={(e) => onFilterChange({ q: e.target.value })}
          aria-label="搜索板块 / 概念"
        />
        <select
          className={styles.select}
          value={phase ?? ''}
          onChange={(e) => onFilterChange({ phase: e.target.value === '' ? null : (e.target.value as ReviewDynamicsPhase) })}
          aria-label="阶段过滤"
        >
          <option value="">阶段：全部</option>
          {PHASE_OPTIONS.map((p) => (
            <option key={p} value={p}>
              {PHASE_LABELS[p]}
            </option>
          ))}
        </select>
        <select
          className={styles.select}
          value={readiness ?? ''}
          onChange={(e) =>
            onFilterChange({ readiness: e.target.value === '' ? null : (e.target.value as ReviewCompositionReadiness) })
          }
          aria-label="数据状态过滤"
        >
          <option value="">数据状态：全部</option>
          {READINESS_OPTIONS.map((r) => (
            <option key={r} value={r}>
              {READINESS_LABELS[r]}
            </option>
          ))}
        </select>
        <select
          className={styles.select}
          value={sort}
          onChange={(e) => onFilterChange({ sort: e.target.value as ReviewSort })}
          aria-label="排序字段"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <div className={styles.viewSwitch} role="group" aria-label="视图切换">
          <button
            type="button"
            className={view === 'table' ? `${styles.viewBtn} ${styles.viewBtnActive}` : styles.viewBtn}
            onClick={() => onViewChange('table')}
          >
            表格
          </button>
          <button
            type="button"
            className={view === 'trajectory' ? `${styles.viewBtn} ${styles.viewBtnActive}` : styles.viewBtn}
            onClick={() => onViewChange('trajectory')}
          >
            轨迹图
          </button>
        </div>
      </div>
    </div>
  )
}
