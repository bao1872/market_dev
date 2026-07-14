// [AdminAppShell] - 描述: 管理员独立布局壳（侧栏管理导航 + 顶栏账户菜单）
// 仅承载 /admin/*，继续使用 AdminRoute 后端权限上下文（is_admin 守卫在 App.tsx）。
// 与普通用户 UserAppShell 完全独立，不共用导航；保留侧栏以承载管理导航与系统状态。
// Capture 路由不经过本壳层。
//
// CHANGE-20260714-001: 侧栏 + 小屏 topbar 增加显式"← 返回行情"入口（NavLink → /market），
// 与 AccountMenu 中的"返回行情"共存，提升管理员退出后台的可发现性。
import { type ReactNode } from 'react'
import { NavLink, Outlet, Link } from 'react-router-dom'
import { useHealth, useAdminSystemOverview } from '@/hooks/useApi'
import { ADMIN_NAV_ITEMS, APP_ROUTES } from '@/navigation/appNavigation'
import BrandLogo from '@/components/BrandLogo'
import AccountMenu from '@/components/AccountMenu'
import clsx from 'clsx'

// 作为路由 layout element 时无 children prop，由 <Outlet/> 渲染子路由；
// 作为普通组件包裹内容时也可传入 children（兼容直接调用场景）。
export default function AdminAppShell({ children }: { children?: ReactNode }) {
  // 真实后端健康状态（普通用户视图隐藏，仅管理员壳层展示）
  const healthQuery = useHealth()
  const isServiceHealthy = healthQuery.data?.status === 'ok'
  // 管理员系统概览（详细状态）- AdminAppShell 仅管理员可进入，直接启用
  const adminOverviewQuery = useAdminSystemOverview(true)
  const adminOverview = adminOverviewQuery.data

  return (
    <div className="app-shell">
      <aside className="sidebar sidebar-desktop">
        <div className="brand">
          <BrandLogo variant="sidebar" />
          <div>
            <div className="brand-title">盘迹</div>
            <div className="brand-sub">ADMIN CONSOLE</div>
          </div>
        </div>

        {/* CHANGE-20260714-001: 侧栏始终可见的"返回行情"入口 */}
        <div className="nav-section">
          <Link to={APP_ROUTES.market} className="nav-item admin-back-to-market" aria-label="返回行情">
            <span>← 返回行情</span>
          </Link>
        </div>

        <div className="nav-section">
          <div className="nav-label">管理员控制台</div>
          {ADMIN_NAV_ITEMS.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === '/admin'}
              className={({ isActive }) => clsx('nav-item', isActive && 'active')}
            >
              <span>{item.label}</span>
            </NavLink>
          ))}
        </div>

        <div className="sidebar-footer">
          <div className="system-mini">
            <div className="system-mini-row">
              <span>
                <i className={clsx('dot', isServiceHealthy ? 'ok' : 'err')}></i>服务状态
              </span>
              <span>{healthQuery.isLoading ? '检测中' : isServiceHealthy ? '正常' : '异常'}</span>
            </div>
            <div className="system-mini-row">
              <span>
                <i className={clsx('dot', adminOverview?.worker_health === 'healthy' ? 'ok' : 'warn')}></i>
                策略引擎
              </span>
              <span>{adminOverview ? (adminOverview.worker_health === 'healthy' ? '正常' : '降级') : '加载中'}</span>
            </div>
            <div className="system-mini-row">
              <span>
                <i className={clsx('dot', adminOverview?.scheduler_health === 'healthy' ? 'ok' : 'warn')}></i>
                任务调度
              </span>
              <span>{adminOverview ? (adminOverview.scheduler_health === 'healthy' ? '正常' : '降级') : '加载中'}</span>
            </div>
            <div className="system-mini-row">
              <span>
                <i className={clsx('dot', (adminOverview?.queue_backlog ?? 0) < 10 ? 'ok' : 'warn')}></i>
                消息队列
              </span>
              <span>{adminOverview?.queue_backlog ?? '-'}</span>
            </div>
          </div>
        </div>
      </aside>

      <header className="topbar">
        <div className="top-left">
          {/* CHANGE-20260714-001: 小屏 topbar 也显示"返回行情"入口（侧栏在小屏隐藏） */}
          <Link to={APP_ROUTES.market} className="admin-back-to-market-topbar" aria-label="返回行情">
            ← 返回行情
          </Link>
          <div className="page-crumb">管理后台</div>
        </div>
        <div className="top-right">
          <AccountMenu variant="admin" />
        </div>
      </header>

      <main className="main">
        <div className="content">{children ?? <Outlet />}</div>
      </main>
    </div>
  )
}
