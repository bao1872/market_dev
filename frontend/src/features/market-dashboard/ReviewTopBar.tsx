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
// - 本组件只拥有：左侧 DashboardTabs + 右侧 muted 日期。不含任何 tab-specific 控件。
import DashboardTabs from './DashboardTabs'
import styles from './dashboard.module.scss'

export default function ReviewTopBar({ projectionDate }: { projectionDate: string | null | undefined }) {
  return (
    <div className={styles.reviewTopBar} data-testid="review-top-bar">
      <DashboardTabs />
      <span className={styles.reviewTopDate} data-testid="review-top-date">
        数据日期 {projectionDate ?? '—'}
      </span>
    </div>
  )
}
