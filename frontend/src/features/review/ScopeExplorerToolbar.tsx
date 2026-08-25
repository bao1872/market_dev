// [ScopeExplorerToolbar] - 描述: Scope Explorer 顶部工具栏（Slice D + R2A）
// 仅展示已激活 canonical family（industry_l1/l2/l3/concept）；
// 不暴露 market/major_index/style；无“全部类型”模式。
// q / phase 为展示层过滤状态。readiness / sort 不再由顶部工具栏渲染：
// - readiness 过滤控件已按 REVIEW-UX-CLOSURE-02 P0-1 删除（canonical 字段保留在 URL/API）。
// - sort 排序已按 P0-2 改为表头排序，工具栏不再含排序下拉框。
// REVIEW-UX-CN-01：所有用户可见 label 中文化；canonical value（phase）不变。
import type { ReviewScopeFamily, ReviewDynamicsPhase } from './types'
import type { ReviewExplorerView, ReviewUrlState } from './urlState'
import { PHASE_LABELS } from './reviewCopy'
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

export interface ScopeExplorerToolbarProps {
  family: ReviewScopeFamily
  view: ReviewExplorerView
  q: string
  phase: ReviewDynamicsPhase | null
  onFamilyChange: (family: ReviewScopeFamily) => void
  onViewChange: (view: ReviewExplorerView) => void
  onFilterChange: (patch: Partial<ReviewUrlState>) => void
}

export default function ScopeExplorerToolbar({
  family,
  view,
  q,
  phase,
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
