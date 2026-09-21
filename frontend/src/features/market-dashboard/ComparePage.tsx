// [MarketDashboard] - 板块比较页（/review/compare）
// [REVIEW-V2-R1] R1 仅建立 canonical 路由并渲染空态：四页完整 UI（含比较矩阵）在 R3 交付。
// 不伪造 projection 数据 —— 在 R3 之前如实展示「尚未提供」。
import DashboardTabs from './DashboardTabs'
import DashboardState from './DashboardState'
import styles from './dashboard.module.scss'

export default function ComparePage() {
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.pageTitle}>板块比较</h1>
        <DashboardTabs />
      </div>
      <DashboardState kind="empty" desc="板块比较将在后续版本提供" />
    </div>
  )
}
