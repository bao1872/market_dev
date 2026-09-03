// [ScopeDetailTabs] - 描述: Review v1 收口后的 Scope Detail 子 Tab（Slice 6）
//
// 合同（PRD §2、§15 + Slice 6 收口）：
// - 正式一级 Tab 只保留 4 个 canonical：dsa / smc / momentum / price。
// - `facts`（原始事实）降级为「更多 › 原始事实」调试入口，不占一级导航。
// - 旧 tab（current / dynamics / internal / leadership / attribution）退出一级导航，
//   仅作为 legacy URL 兼容值存在（见 urlState）；本组件不再渲染其入口。
// - tab 状态只来自 canonical URL（SSOT），组件无本地副本。
// - 点击 tab 只 patch `tab`，preserve date/family/scopeKey/view/phase/readiness/sort/page/pageSize/q。
// - brand green 仅用于当前激活 tab（focus/focus）。
// REVIEW-UX-CN-01：tab label 中文化（reviewCopy.DETAIL_TAB_LABELS）+ hover tooltip
// （reviewCopy.DETAIL_TAB_HELP，经 ReviewTerm compact 渲染，无 ⓘ 图标）。
//
// 可访问性（UX19）：role="tab" 的 button 自身持有 aria-describedby，内部
// ReviewTerm 设 focusable={false}（不引入第二个 tabIndex=0 stop）。「更多」用原生
// <details>/<summary>，不属于 tablist，避免破坏 tablist 键盘模型。
import type { ReviewDetailTab } from './urlState'
import ReviewTerm from './ReviewTerm'
import { DETAIL_TAB_LABELS, DETAIL_TAB_HELP } from './reviewCopy'
import styles from './review.module.scss'

export interface ScopeDetailTabDef {
  value: ReviewDetailTab
  label: string
  help: string
}

/** 正式一级 Tab（canonical，普通用户唯一可见的 Tab 集） */
export const SCOPE_DETAIL_TABS: ReadonlyArray<ScopeDetailTabDef> = [
  { value: 'dsa', label: DETAIL_TAB_LABELS.dsa, help: DETAIL_TAB_HELP.dsa },
  { value: 'smc', label: DETAIL_TAB_LABELS.smc, help: DETAIL_TAB_HELP.smc },
  { value: 'momentum', label: DETAIL_TAB_LABELS.momentum, help: DETAIL_TAB_HELP.momentum },
  { value: 'price', label: DETAIL_TAB_LABELS.price, help: DETAIL_TAB_HELP.price },
]

/** 调试入口（退出一级导航，仅「更多」下可见） */
export const SCOPE_DEBUG_TABS: ReadonlyArray<ScopeDetailTabDef> = [
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

      {/* 「更多」调试入口：原生 disclosure，不属于 tablist */}
      <details className={styles.detailMore}>
        <summary className={styles.detailMoreSummary}>更多</summary>
        <div className={styles.detailMoreMenu} role="menu">
          {SCOPE_DEBUG_TABS.map((def) => {
            const active = def.value === tab
            return (
              <button
                key={def.value}
                type="button"
                role="menuitem"
                aria-label={`${def.label}（调试）`}
                title={def.help}
                className={active ? `${styles.detailTab} ${styles.detailTabActive}` : styles.detailTab}
                onClick={() => onTabChange(def.value)}
              >
                {def.label}
              </button>
            )
          })}
        </div>
      </details>
    </div>
  )
}
