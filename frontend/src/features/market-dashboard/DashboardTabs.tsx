// [MarketDashboard][R3A] - 二级导航：大盘 / 行业 / 概念
//
// 路由即页面身份（不用 query 参数切换）。对比页不再进入顶层 tab，
// 入口为各详情页「查看对比」链接（/review/compare）。
import { NavLink, useLocation } from 'react-router-dom'
import { REVIEW_TABS, reviewTabLabel } from './reviewTabs'
import styles from './dashboard.module.scss'

export default function DashboardTabs() {
  const { pathname } = useLocation()

  return (
    <nav className={styles.tabs} aria-label="复盘视图">
      {REVIEW_TABS.map((tab) => {
        const active = pathname === tab.path
        return (
          <NavLink
            key={tab.key}
            to={tab.path}
            className={active ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            aria-current={active ? 'page' : undefined}
          >
            {reviewTabLabel(tab)}
          </NavLink>
        )
      })}
    </nav>
  )
}
