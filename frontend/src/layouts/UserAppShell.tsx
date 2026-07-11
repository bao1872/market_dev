// [UserAppShell] - 描述: 普通用户布局壳（顶栏品牌 + 一级导航 + 账户菜单；无左侧栏）
// 承载：/market（行情，渲染 MarketWorkspacePage）、/replay（复盘占位）、/stock/:symbol、/messages、/settings
// 不再渲染旧 AppShell 的统一侧栏；消息/设置已收拢到 AccountMenu。
// 市场状态 30s 轮询 + 上海时区实时钟（原 AppShell 职责迁移至此）。
// Capture 路由不经过本壳层（由 App.tsx 独立路由处理）。
import { type ReactNode, useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useRoleStore } from '@/store/role'
import { getMarketStatus, type MarketStatus } from '@/api/endpoints'
import { setCachedMarketStatus } from '@/hooks/useApi'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { USER_NAV_ITEMS } from '@/navigation/appNavigation'
import BrandLogo from '@/components/BrandLogo'
import AccountMenu from '@/components/AccountMenu'
import clsx from 'clsx'
import styles from './UserAppShell.module.scss'

// 作为路由 layout element 时无 children prop，由 <Outlet/> 渲染子路由；
// 作为普通组件包裹内容时也可传入 children（兼容直接调用场景）。
export default function UserAppShell({ children }: { children?: ReactNode }) {
  const { isAdmin, toggleRole } = useRoleStore()

  // 市场状态轮询（30s）- 同步更新模块级缓存供 isInTradingHours() 使用
  const [marketStatus, setMarketStatus] = useState<MarketStatus | null>(null)
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const status = await getMarketStatus()
        setMarketStatus(status)
        setCachedMarketStatus(status)
      } catch {
        // API 失败时保持当前状态（缓存不更新，isInTradingHours 自动 fallback 到本地判断）
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

  return (
    <div className={clsx('app-shell', styles.userShell)}>
      <header className="topbar">
        <div className="top-left">
          <NavLink to="/market" className={styles.brandLink} aria-label="盘迹行情首页">
            <BrandLogo variant="sidebar" />
          </NavLink>
          <nav className={styles.nav} aria-label="主导航">
            {USER_NAV_ITEMS.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                className={({ isActive }) => clsx(styles.navLink, isActive && styles.navLinkActive)}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="top-status">
            <i className={marketStatus?.is_trading_hours ? 'dot ok' : 'dot'}></i>
            A股{marketStatus?.status_text ?? '加载中'} · {currentTime}
          </div>
        </div>
        <div className="top-right">
          {/* 角色预览切换（仅测试用，普通用户路径不渲染管理员导航） */}
          {!isAdmin && (
            <button
              className="btn small role-preview-toggle"
              onClick={toggleRole}
              title={isAdmin ? '仅用于检查普通用户权限界面' : '恢复默认管理员测试账号'}
            >
              {isAdmin ? '切换普通用户视图' : '返回管理员视图'}
            </button>
          )}
          <AccountMenu variant="user" />
        </div>
      </header>
      <main className="main">
        <div className="content">{children ?? <Outlet />}</div>
      </main>
    </div>
  )
}
