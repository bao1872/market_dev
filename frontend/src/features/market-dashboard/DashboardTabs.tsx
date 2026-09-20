// [MarketDashboard] - 二级 Tab（大盘 / 行业板块 / 概念板块），路由即页面身份，不用 query 参数切换
import { NavLink, useLocation } from 'react-router-dom'
import { MARKET_DASHBOARD_ROUTES } from './types'
import styles from './dashboard.module.scss'

const TABS = [
  { label: '大盘', path: MARKET_DASHBOARD_ROUTES.market },
  { label: '行业板块', path: MARKET_DASHBOARD_ROUTES.industry },
  { label: '概念板块', path: MARKET_DASHBOARD_ROUTES.concept },
]

export default function DashboardTabs() {
  const { pathname } = useLocation()
  return (
    <div className={styles.tabs}>
      {TABS.map((tab) => {
        const active = pathname === tab.path
        return (
          <NavLink
            key={tab.path}
            to={tab.path}
            className={active ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          >
            {tab.label}
          </NavLink>
        )
      })}
    </div>
  )
}
