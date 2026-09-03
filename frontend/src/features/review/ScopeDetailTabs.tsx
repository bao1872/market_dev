// [ScopeDetailTabs] - 描述: Canonical Scope Detail 五个精确子 Tab（Slice E）
//
// 合同（prompt §2、§15）：
// - 六个 tab：dynamics / current / internal / leadership / attribution / facts（current 为 R1 新增合法 tab）。
// - tab 状态只来自 canonical URL（SSOT），组件无本地副本。
// - 点击 tab 只 patch `tab`，preserve date/family/scopeKey/view/phase/readiness/sort/page/pageSize/q。
// - brand green 仅用于当前激活 tab（focus/focus）。
// REVIEW-UX-CN-01：tab label 中文化（reviewCopy.DETAIL_TAB_LABELS）+ hover tooltip
// （reviewCopy.DETAIL_TAB_HELP，经 ReviewTerm compact 渲染，无 ⓘ 图标）。
import type { ReviewDetailTab } from './urlState'
import ReviewTerm from './ReviewTerm'
import { DETAIL_TAB_LABELS, DETAIL_TAB_HELP } from './reviewCopy'
import styles from './review.module.scss'

export interface ScopeDetailTabDef {
  value: ReviewDetailTab
  label: string
  help: string
}

export const SCOPE_DETAIL_TABS: ReadonlyArray<ScopeDetailTabDef> = [
  { value: 'dsa', label: DETAIL_TAB_LABELS.dsa, help: DETAIL_TAB_HELP.dsa },
  { value: 'smc', label: DETAIL_TAB_LABELS.smc, help: DETAIL_TAB_HELP.smc },
  { value: 'momentum', label: DETAIL_TAB_LABELS.momentum, help: DETAIL_TAB_HELP.momentum },
  { value: 'price', label: DETAIL_TAB_LABELS.price, help: DETAIL_TAB_HELP.price },
  { value: 'current', label: DETAIL_TAB_LABELS.current, help: DETAIL_TAB_HELP.current },
  { value: 'dynamics', label: DETAIL_TAB_LABELS.dynamics, help: DETAIL_TAB_HELP.dynamics },
  { value: 'internal', label: DETAIL_TAB_LABELS.internal, help: DETAIL_TAB_HELP.internal },
  { value: 'leadership', label: DETAIL_TAB_LABELS.leadership, help: DETAIL_TAB_HELP.leadership },
  { value: 'attribution', label: DETAIL_TAB_LABELS.attribution, help: DETAIL_TAB_HELP.attribution },
  { value: 'facts', label: DETAIL_TAB_LABELS.facts, help: DETAIL_TAB_HELP.facts },
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
        const tabTooltipId = `review-detail-tab-tooltip-${def.value}`
        return (
          <button
            key={def.value}
            type="button"
            role="tab"
            aria-selected={active}
            aria-label={`${def.label} 子 Tab`}
            aria-describedby={tabTooltipId}
            className={active ? `${styles.detailTab} ${styles.detailTabActive}` : styles.detailTab}
            onClick={() => onTabChange(def.value)}
          >
            <ReviewTerm
              label={def.label}
              help={def.help}
              compact
              focusable={false}
              tooltipId={tabTooltipId}
            />
          </button>
        )
      })}
    </div>
  )
}