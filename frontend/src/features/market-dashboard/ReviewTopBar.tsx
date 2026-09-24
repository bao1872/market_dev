// [PANJI-REVIEW-UI-RUNTIME-PARITY-FIX][§2] 复盘视图统一下一级顶部上下文行。
//
// 三个 tab（大盘 / 行业 / 概念）渲染**完全相同**的一行：
//   [大盘] [行业] [概念]                          数据日期 <projection date>
//
// 语义约束：
// - **不再有页面级 H1「市场复盘」**：全局导航已把模块标为「复盘」，页内重复标题没有信息增量，
//   且会让复盘比 /market 多一层信息层级。复盘内容直接以本行为起点。
// - projection date 由各自页面传入（canonical 响应字段），本组件不自行取数、不重复后端逻辑，
//   也**不再**是页面唯一日期 owner 之外的第二个来源。
// - 本组件只拥有：DashboardTabs（tab 唯一 owner）+ 可选 controls slot + 右侧 muted 日期。
//   tab-specific 控件由调用方通过 controls 注入，本组件不认识任何业务控件。
//
// [PANJI-REVIEW-EXPLORER-CONTROL-DECK-01] variant：
// - default：大盘页（MarketDashboardPage）保持原行为（tabs → date，两翼分布）。
// - deck：行业 / 概念 Explorer 的第一层控制流（tabs → controls → date，连续一行，无固定右对齐留白）。
import type { ReactNode } from 'react'
import clsx from 'clsx'
import DashboardTabs from './DashboardTabs'
import styles from './dashboard.module.scss'

export interface ReviewTopBarProps {
  projectionDate: string | null | undefined
  /** tab 专属控件（层级 + 搜索）：只有 Explorer 注入；大盘页不传。 */
  controls?: ReactNode
  variant?: 'default' | 'deck'
}

export default function ReviewTopBar({ projectionDate, controls, variant = 'default' }: ReviewTopBarProps) {
  return (
    <div
      className={clsx(styles.reviewTopBar, variant === 'deck' && styles.reviewTopBarDeck)}
      data-testid="review-top-bar"
      data-variant={variant}
    >
      <DashboardTabs />

      {controls && (
        <div className={styles.reviewTopControls} data-testid="review-top-controls">
          {controls}
        </div>
      )}

      <span className={styles.reviewTopDate} data-testid="review-top-date">
        数据日期 {projectionDate ?? '—'}
      </span>
    </div>
  )
}
