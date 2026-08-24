// [ScopeDetailTabs] - 描述: Canonical Scope Detail 五个精确子 Tab（Slice E）
//
// 合同（prompt §2、§15）：
// - 六个 tab：dynamics / current / internal / leadership / attribution / facts（current 为 R1 新增合法 tab）。
// - tab 状态只来自 canonical URL（SSOT），组件无本地副本。
// - 点击 tab 只 patch `tab`，preserve date/family/scopeKey/view/phase/readiness/sort/page/pageSize/q。
// - brand green 仅用于当前激活 tab（focus/focus）。
import type { ReviewDetailTab } from './urlState'
import styles from './review.module.scss'

export interface ScopeDetailTabDef {
  value: ReviewDetailTab
  label: string
}

export const SCOPE_DETAIL_TABS: ReadonlyArray<ScopeDetailTabDef> = [
  { value: 'current', label: 'Current' },
  { value: 'dynamics', label: 'Dynamics' },
  { value: 'internal', label: 'Internal' },
  { value: 'leadership', label: 'Leadership' },
  { value: 'attribution', label: 'Attribution' },
  { value: 'facts', label: 'Facts' },
]

export interface ScopeDetailTabsProps {
  /** 当前激活 tab（来自 URL；不在此处维护本地状态） */
  tab: ReviewDetailTab
  onTabChange: (tab: ReviewDetailTab) => void
}

export default function ScopeDetailTabs({ tab, onTabChange }: ScopeDetailTabsProps) {
  return (
    <div className={styles.detailTabs} role="tablist" aria-label="Scope 详情子 Tab">
      {SCOPE_DETAIL_TABS.map((def) => {
        const active = def.value === tab
        return (
          <button
            key={def.value}
            type="button"
            role="tab"
            aria-selected={active}
            aria-label={`${def.label} 子 Tab`}
            className={active ? `${styles.detailTab} ${styles.detailTabActive}` : styles.detailTab}
            onClick={() => onTabChange(def.value)}
          >
            {def.label}
          </button>
        )
      })}
    </div>
  )
}