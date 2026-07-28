// [UserAppShell] - 描述: 普通用户布局壳（顶栏品牌 + 一级导航 + 账户菜单；无左侧栏）
// 承载：/market（行情/自选，渲染 MarketWorkspacePage）、/replay（复盘占位）、/stock/:symbol、/messages、/settings
// [Round 2026-07-28-4] 一级导航：行情｜自选｜复盘
//   - 行情和自选都指向 /market，通过 scope=watchlist 区分
//   - UserAppShell 不依赖 NavLink pathname 判断 active，使用 resolveActiveNav
//   - 点击行情/自选时通过 buildScopeSwitchUrl 保留筛选条件
import { type ReactNode, useEffect, useState, useMemo } from 'react'
import { NavLink, Outlet, useLocation, useSearchParams } from 'react-router-dom'
import { getMarketStatus, type MarketStatus } from '@/api/endpoints'
import { setCachedMarketStatus } from '@/hooks/useApi'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import {
  USER_NAV_ITEMS,
  APP_ROUTES,
  WATCHLIST_NAV_PATH,
  resolveActiveNav,
  buildScopeSwitchUrl,
} from '@/navigation/appNavigation'
import { useAuthStore } from '@/store/auth'
import BrandLogo from '@/components/BrandLogo'
import AccountMenu from '@/components/AccountMenu'
import clsx from 'clsx'
import styles from './UserAppShell.module.scss'

// 作为路由 layout element 时无 children prop，由 <Outlet/> 渲染子路由；
// 作为普通组件包裹内容时也可传入 children（兼容直接调用场景）。
export default function UserAppShell({ children }: { children?: ReactNode }) {
  const location = useLocation()
  const [searchParams] = useSearchParams()
  // 市场状态轮询（30s）- 同步更新模块级缓存供 isInTradingHours() 使用
  const [marketStatus, setMarketStatus] = useState<MarketStatus | null>(null)
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const status = await getMarketStatus()
        setMarketStatus(status)
        setCachedMarketStatus(status)
      } catch {
        // API 失败时保持当前状态
      }
    }
    fetchStatus()
    const interval = setInterval(fetchStatus, 30000)
    return () => clearInterval(interval)
  }, [])

  // 实时时钟（1s 刷新，固定上海时区）
  const [currentTime, setCurrentTime] = useState(formatShanghaiTimeShort(new Date()))
  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentTime(formatShanghaiTimeShort(new Date()))
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  // 权限：自选仅 admin 或 self_selection active 可见
  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)
  const hasSelfSelection = useAuthStore((s) => !!s.user?.capabilities?.self_selection?.active)
  const canAccessWatchlist = isAdmin || hasSelfSelection

  // 过滤可见导航项：无自选权限时隐藏"自选"
  const visibleNavItems = useMemo(() => {
    return USER_NAV_ITEMS.filter((item) => {
      if (item.path === WATCHLIST_NAV_PATH) return canAccessWatchlist
      return true
    })
  }, [canAccessWatchlist])

  // 构建导航链接的 to 路径：行情/自选需要保留当前筛选条件
  const buildNavTo = (itemPath: string): string => {
    if (itemPath === APP_ROUTES.market) {
      // 点击"行情"：切换到 scope=market，保留筛选
      return buildScopeSwitchUrl(searchParams, 'market')
    }
    if (itemPath === WATCHLIST_NAV_PATH) {
      // 点击"自选"：切换到 scope=watchlist，保留筛选
      return buildScopeSwitchUrl(searchParams, 'watchlist')
    }
    return itemPath
  }

  return (
    <div className={clsx('app-shell', styles.userShell)}>
      <header className="topbar">
        <div className="top-left">
          <NavLink to="/market" className={styles.brandLink} aria-label="盘迹行情首页">
            <BrandLogo variant="sidebar" />
          </NavLink>
          <nav className={styles.nav} aria-label="主导航">
            {visibleNavItems.map((item) => {
              const to = buildNavTo(item.path)
              const active = resolveActiveNav(location.pathname, searchParams, item.path)
              return (
                <NavLink
                  key={item.path}
                  to={to}
                  className={clsx(styles.navLink, active && styles.navLinkActive)}
                >
                  {item.label}
                </NavLink>
              )
            })}
          </nav>
          <div className="top-status">
            <i className={marketStatus?.is_trading_hours ? 'dot ok' : 'dot'}></i>
            A股{marketStatus?.status_text ?? '加载中'} · {currentTime}
          </div>
        </div>
        <div className="top-right">
          <AccountMenu variant="user" />
        </div>
      </header>
      <main className="main">
        <div className="content">{children ?? <Outlet />}</div>
      </main>
    </div>
  )
}
