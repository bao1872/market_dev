// [MarketDashboard][R3A] - 二级导航：大盘 / 行业 / 概念 / 对比 N
//
// 路由即页面身份（不用 query 参数切换）；对比 tab 的 N = 共享比较篮数量，
// 因此 /review/industry、/review/concept、/review/compare 之间导航时数量保持一致。
import { NavLink, useLocation } from 'react-router-dom'
import { useCompareBasketStore } from '@/store/compareBasket'
import { REVIEW_TABS, reviewTabLabel } from './reviewTabs'
import styles from './dashboard.module.scss'

export default function DashboardTabs() {
  const { pathname } = useLocation()
  const basketCount = useCompareBasketStore((state) => state.items.length)

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
            {reviewTabLabel(tab, basketCount)}
          </NavLink>
        )
      })}
    </nav>
  )
}
