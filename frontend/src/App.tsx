// [Auth] - 描述: 路由配置 + 受保护路由守卫 + Admin/Subscriber 角色守卫
// 公开路由：/（门户页，lazy 加载）, /login, /subscription-expired（canonical），/membership-expired（重定向）
// 受保护路由：认证由 ProtectedLayout 负责（仅校验 auth + access profile，不再固定渲染同一壳层）
// 布局壳拆分（PRD V1.0 阶段一）：
//   UserAppShell   承载普通用户 /market /replay /stock/:symbol /messages /settings
//   AdminAppShell  承载管理员 /admin/*（继续使用 AdminRoute 后端权限上下文）
//   /capture/stock/:symbol 位于两套壳层之外（只使用 captureClient，不经过任何壳层）
// SubscriberRoute：有效订阅或 admin 豁免，否则重定向到 /subscription-expired
// AdminRoute：is_admin=true 才可访问，否则重定向到 /market（替换旧 /overview）
import { lazy, Suspense, useEffect, useRef } from 'react'
import { Navigate, Outlet, type RouteObject, useParams } from 'react-router-dom'
import { useAuthStore, ACCESS_TOKEN_KEY } from './store/auth'
import type { AuthUser } from './store/auth'
import { canAccessMarket, canAccessStockDetail, canAccessReplay } from './auth/capabilityAccess'
import UserAppShell from './layouts/UserAppShell'
import AdminAppShell from './layouts/AdminAppShell'
import { legacyRedirectEntries, DEFAULT_ENTRY } from './navigation/appNavigation'
import LoginPage from './pages/LoginPage'
import SubscriptionExpiredPage from './pages/SubscriptionExpiredPage'
import MarketWorkspacePage from './features/market-workspace/MarketWorkspacePage'
import ReplayPage from './pages/ReplayPage'
import StockDetailPage from './pages/StockDetailPage'
import CaptureStockPage from './pages/CaptureStockPage'
import SettingsPage from './pages/SettingsPage'
import MessagesPage from './pages/MessagesPage'
import AdminIndexPage from './pages/AdminIndexPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminJobsPage from './pages/AdminJobsPage'
import AdminBetaApplicationsPage from './pages/AdminBetaApplicationsPage'
import AdminAfterClosePipelinePage from './pages/AdminAfterClosePipelinePage'
import AdminStockDebugPage from './pages/AdminStockDebugPage'
import AdminInviteCapabilityPage from './pages/AdminInviteCapabilityPage'

// 门户页 lazy 加载，避免门户动画代码进入业务页面首包
const LandingPage = lazy(() => import('./pages/LandingPage'))

// 门户页加载占位
function LandingFallback() {
  return <div style={{ minHeight: '100vh', background: '#030915' }} />
}

// 受保护路由布局：仅负责认证与 access profile，不再渲染统一 AppShell
// Capture token 处理已彻底移除：capture=feishu 路由位于 ProtectedLayout 之外，
// 由独立 CaptureStockPage 处理 token。普通受保护路由即使携带 capture 参数也绝不能清除 ACCESS_TOKEN_KEY。
function ProtectedLayout() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)

  // [Auth] - 描述: 刷新页面后校验权限上下文（防止 persist 的 subscription_active 过期）
  // 仅执行一次（useRef 守卫避免路由切换重复触发）
  const revalidatedRef = useRef(false)
  useEffect(() => {
    if (revalidatedRef.current) return
    revalidatedRef.current = true
    void revalidateAccess()
  }, [revalidateAccess])

  // 双重检查：zustand isAuthenticated + localStorage auth_token
  // 防止 token 过期后 isAuthenticated 仍为 true 但 auth_token 已被清除
  const hasToken = !!localStorage.getItem(ACCESS_TOKEN_KEY)
  if (!isAuthenticated || !hasToken) {
    return <Navigate to="/login" replace />
  }
  return <Outlet />
}

// [V2.1] CapabilityRoute - 基于 V2.1 capabilities 的路由守卫（PRD §9, §10）
// 前端只消费 /me/access 返回的 capabilities + watchlist_limits，不从旧 features/monitor_limit 推导。
// accessLoading 期间显示 loading，避免 revalidateAccess 未返回时提前判定 false。
// 后端仍为最终 403 边界：手工 URL 访问无权限页面时，前端重定向到 /no-permission，后端 API 仍返回 403。
function CapabilityRoute({ check }: { check: (user: AuthUser | null) => boolean }) {
  const user = useAuthStore((s) => s.user)
  const accessLoading = useAuthStore((s) => s.accessLoading)
  // access revalidation 进行中且 user 为 null 或 V2.1 字段未填充时等待
  if (accessLoading) {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }
  if (!check(user)) {
    return <Navigate to="/no-permission" replace />
  }
  return <Outlet />
}

// [V2.1] 无权限提示页（手工 URL 访问无权限路由时展示）
function ForbiddenPage() {
  return (
    <div style={{ minHeight: '100vh', background: '#0A0F14', color: '#E5E7EB', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 16 }}>
      <h1 style={{ fontSize: 24, margin: 0 }}>无权限访问</h1>
      <p style={{ margin: 0, color: '#9CA3AF' }}>当前账号未开通该功能权限</p>
      <a href="/market" style={{ color: '#3B82F6' }}>返回行情</a>
    </div>
  )
}

// [Auth] - 描述: AdminRoute 管理员守卫 - 使用 is_admin 字段判断（替代旧 user.role）
// 非 admin 用户重定向到默认入口 /market（替换旧 /overview）
// accessLoading 期间显示轻量 loading，避免 /me/access 未返回时提前判定 false
function AdminRoute() {
  const user = useAuthStore((s) => s.user)
  const accessLoading = useAuthStore((s) => s.accessLoading)
  // access revalidation 进行中时等待结果（防止刷新页面时 user 尚未更新而错误重定向）
  if (accessLoading && !user?.is_admin) {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }
  if (user?.is_admin !== true) {
    return <Navigate to="/market" replace />
  }
  return <Outlet />
}

// 旧路由兼容重定向（/overview → /market，/watchlist → /market?scope=watchlist，/screener → /market）
const redirectRoutes = legacyRedirectEntries().map(({ path, to }) => ({
  path,
  element: <Navigate to={to} replace />,
}))

// [Phase4] 旧管理员调试动态路由重定向：/admin/stock-debug/:symbol → /admin/stocks/:symbol/debug
function OldStockDebugRedirect() {
  const { symbol } = useParams<{ symbol: string }>()
  return <Navigate to={`/admin/stocks/${symbol}/debug`} replace />
}

// 导出纯路由配置结构（供路由契约测试断言，不依赖 React 渲染）
export const routeConfig: RouteObject[] = [
  // 公开路由
  { path: '/', element: <Suspense fallback={<LandingFallback />}><LandingPage /></Suspense> },
  { path: '/login', element: <LoginPage /> },
  // [Auth] - 描述: /subscription-expired 为 canonical 路由，/membership-expired 重定向到此（向后兼容）
  { path: '/subscription-expired', element: <SubscriptionExpiredPage /> },
  { path: '/membership-expired', element: <Navigate to="/subscription-expired" replace /> },
  // [capture-mode] 专用 Capture 路由（不经过 ProtectedLayout/SubscriberRoute/UserAppShell/AdminAppShell，只使用 captureClient）
  // capture worker 通过 /capture/stock/:symbol?capture=feishu&token=xxx 访问，避免加载 watchlist/memo/events
  // Capture token 只在 CaptureStockPage 内部处理，ProtectedLayout 不再解析 capture 参数或操作 localStorage
  { path: '/capture/stock/:symbol', element: <CaptureStockPage /> },
  // 受保护路由组
  {
    element: <ProtectedLayout />,
    children: [
      // 普通用户界面（UserAppShell 布局）
      {
        element: <UserAppShell />,
        children: [
          // [V2.1] 基于 capability 的业务路由守卫（PRD §9, §10）
          // /market: watchlist_management 或 market_screening 任一即可
          {
            element: <CapabilityRoute check={canAccessMarket} />,
            children: [
              { path: '/market', element: <MarketWorkspacePage /> },
            ],
          },
          // /stock/:symbol + /replay: 需要 market_screening（详情/DSA/K线/复盘）
          {
            element: <CapabilityRoute check={canAccessStockDetail} />,
            children: [
              { path: '/stock/:symbol', element: <StockDetailPage /> },
            ],
          },
          {
            element: <CapabilityRoute check={canAccessReplay} />,
            children: [
              { path: '/replay', element: <ReplayPage /> },
            ],
          },
          // 不强制订阅的辅助页面（仅认证即可）
          { path: '/settings', element: <SettingsPage /> },
          { path: '/messages', element: <MessagesPage /> },
        ],
      },
      // 管理员界面（AdminAppShell 独立布局）
      {
        element: <AdminRoute />,
        children: [
          {
            element: <AdminAppShell />,
            children: [
              // [Auth] - 描述: /admin/overview 为后端 next_route 返回值，与 /admin 同渲染 AdminIndexPage
              { path: '/admin', element: <AdminIndexPage /> },
              { path: '/admin/overview', element: <AdminIndexPage /> },
              { path: '/admin/users', element: <AdminUsersPage /> },
              { path: '/admin/beta-applications', element: <AdminBetaApplicationsPage /> },
              // C8: 策略目录页已废弃，重定向到盘后流水线（DSA 运行能力保留在此）
              { path: '/admin/strategies', element: <Navigate to="/admin/after-close" replace /> },
              { path: '/admin/jobs', element: <AdminJobsPage /> },
              { path: '/admin/after-close', element: <AdminAfterClosePipelinePage /> },
              { path: '/admin/stocks', element: <AdminStockDebugPage /> },
              { path: '/admin/stocks/:symbol/debug', element: <AdminStockDebugPage /> },
              { path: '/admin/invite-codes', element: <AdminInviteCapabilityPage /> },
            ],
          },
        ],
      },
      // 旧路由兼容重定向（保留，避免书签/旧链接 404）
      ...redirectRoutes,
      // [Phase4] 旧管理员调试动态路由重定向
      { path: '/admin/stock-debug/:symbol', element: <OldStockDebugRedirect /> },
      // [V2.1] 无权限提示页（手工 URL 访问无权限路由时由 CapabilityRoute 重定向到此）
      { path: '/no-permission', element: <ForbiddenPage /> },
    ],
  },
  // 兜底：未匹配路由重定向到默认入口（替换旧 /overview）
  { path: '*', element: <Navigate to={DEFAULT_ENTRY} replace /> },
]

// 路由测试辅助：递归查找匹配路径的路由对象（用于断言路由层级关系）
export function findRouteByPath(routes: RouteObject[], path: string): { route: RouteObject; parents: RouteObject[] } | null {
  function search(routeList: RouteObject[], parents: RouteObject[]): { route: RouteObject; parents: RouteObject[] } | null {
    for (const route of routeList) {
      if (route.path === path) {
        return { route, parents }
      }
      if (route.children) {
        const result = search(route.children, [...parents, route])
        if (result) return result
      }
    }
    return null
  }
  return search(routes, [])
}

import { createBrowserRouter } from 'react-router-dom'
export const router = createBrowserRouter(routeConfig)
