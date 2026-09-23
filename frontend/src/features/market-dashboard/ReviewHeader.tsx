// [PANJI-REVIEW-UI-UNIFY][P1-1] 复盘视图统一顶部 shell：大盘 / 行业 / 概念 共用。
//
// 三个 tab 页必须渲染完全相同的 header 结构：
//   市场复盘                        数据日期：<projection date>
//   [大盘] [行业] [概念]
//
// projection date 来自 canonical 响应（useMarketDashboard → projection_trade_date），
// 由各自页面传入，本组件不自行获取、不重复后端逻辑。
import DashboardTabs from './DashboardTabs'
import styles from './dashboard.module.scss'

export default function ReviewHeader({ projectionDate }: { projectionDate: string | null | undefined }) {
  return (
    <>
      <div className={styles['review-head']}>
        <h1 className={styles['page-title']}>市场复盘</h1>
        <span className={styles['proj-date']}>数据日期：{projectionDate ?? '—'}</span>
      </div>
      <DashboardTabs />
    </>
  )
}
