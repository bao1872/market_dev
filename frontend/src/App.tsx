// [Auth] - 描述: 路由配置 + 受保护路由守卫 + Admin/Subscriber 角色守卫
// 公开路由：/（门户页，lazy 加载）, /login, /subscription-expired（canonical），/membership-expired（重定向）
// 受保护路由：认证由 ProtectedLayout 负责（仅校验 auth + access profile，不再固定渲染同一壳层）
// 布局壳拆分（阶段二）：
//   UserAppShell   承载普通用户 /market /screener /stock/:symbol /messages /settings
//   AdminAppShell  承载管理员 /admin/*（继续使用 AdminRoute 后端权限上下文）
//   /capture/stock/:symbol 位于两套壳层之外（只使用 captureClient，不经过任何壳层）
// SubscriberRoute：有效订阅或 admin 豁免，否则重定向到 /subscription-expired
// AdminRoute：is_admin=true 才可访问，否则重定向到 /market（替换旧 /overview）
import { lazy, Suspense, useEffect, useRef } from 'react'
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import { useAuthStore, ACCESS_TOKEN_KEY, CAPTURE_TOKEN_KEY } from './store/auth'
import UserAppShell from './layouts/UserAppShell'
import AdminAppShell from './layouts/AdminAppShell'
import { legacyRedirectEntries, DEFAULT_ENTRY } from './navigation/appNavigation'
import LoginPage from './pages/LoginPage'
import SubscriptionExpiredPage from './pages/SubscriptionExpiredPage'
import WatchlistPage from './pages/WatchlistPage'
import ScreenerPage from './pages/ScreenerPage'
import StockDetailPage from './pages/StockDetailPage'
import CaptureStockPage from './pages/CaptureStockPage'
import SettingsPage from './pages/SettingsPage'
import MessagesPage from './pages/MessagesPage'
import AdminIndexPage from './pages/AdminIndexPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminStrategiesPage from './pages/AdminStrategiesPage'
import AdminJobsPage from './pages/AdminJobsPage'
import AdminBetaApplicationsPage from './pages/AdminBetaApplicationsPage'
import AdminAfterClosePipelinePage from './pages/AdminAfterClosePipelinePage'

// 门户页 lazy 加载，避免门户动画代码进入业务页面首包
const LandingPage = lazy(() => import('./pages/LandingPage'))

// 门户页加载占位
function LandingFallback() {
  return <div style={{ minHeight: '100vh', background: '#030915' }} />
}

// 受保护路由布局：仅负责认证与 access profile，不再渲染统一 AppShell
function ProtectedLayout() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)
  const location = window.location
  const searchParams = new URLSearchParams(location.search)
  // 截图模式：URL 带 capture=feishu 且 token 有效时，允许直接访问（capture 路由本身在 ProtectedLayout 之外，
  // 此处保留写入逻辑以兼容任何经由此布局携带 capture 参数的访问，不污染普通登录态）
  const isCaptureMode = searchParams.get('capture') === 'feishu'
  const captureToken = searchParams.get('token')

  // [capture-mode] 截图模式：将 capture token 写入独立的 capture_token storage key
  // 不写入 auth_token（避免污染普通登录态）；清理可能残留的 auth_token（历史污染遗留）
  if (isCaptureMode && captureToken) {
    localStorage.setItem(CAPTURE_TOKEN_KEY, captureToken)
    localStorage.removeItem(ACCESS_TOKEN_KEY)
  }

  // [Auth] - 描述: 刷新页面后校验权限上下文（防止 persist 的 subscription_active 过期）
  // 仅执行一次（useRef 守卫避免路由切换重复触发），capture 模式由 revalidateAccess 内部跳过
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

// [Auth] - 描述: SubscriberRoute 订阅守卫 - 非有效订阅用户重定向到 /subscription-expired（canonical）
// admin 用户豁免（is_admin=true 直接通过，不强制订阅）
// 用于 /market /screener /stock/:symbol 等需有效订阅的核心业务路由
function SubscriberRoute() {
  const user = useAuthStore((s) => s.user)
  // admin 豁免：管理员无需有效订阅即可访问所有页面
  if (user?.is_admin) {
    return <Outlet />
  }
  // 非订阅用户重定向到续期页
  if (!user?.subscription_active) {
    return <Navigate to="/subscription-expired" replace />
  }
  return <Outlet />
}

// [Auth] - 描述: AdminRoute 管理员守卫 - 使用 is_admin 字段判断（替代旧 user.role）
// 非 admin 用户重定向到默认入口 /market（替换旧 /overview）
function AdminRoute() {
  const user = useAuthStore((s) => s.user)
  if (user?.is_admin !== true) {
    return <Navigate to="/market" replace />
  }
  return <Outlet />
}

// 旧路由兼容重定向（/overview → /market，/watchlist → /market?scope=watchlist）
const redirectRoutes = legacyRedirectEntries().map(({ path, to }) => ({
  path,
  element: <Navigate to={to} replace />,
}))

export const router = createBrowserRouter([
  // 公开路由
  { path: '/', element: <Suspense fallback={<LandingFallback />}><LandingPage /></Suspense> },
  { path: '/login', element: <LoginPage /> },
  // [Auth] - 描述: /subscription-expired 为 canonical 路由，/membership-expired 重定向到此（向后兼容）
  { path: '/subscription-expired', element: <SubscriptionExpiredPage /> },
  { path: '/membership-expired', element: <Navigate to="/subscription-expired" replace /> },
  // [capture-mode] 专用 Capture 路由（不经过 ProtectedLayout/SubscriberRoute/UserAppShell/AdminAppShell，只使用 captureClient）
  // capture worker 通过 /capture/stock/:symbol?capture=feishu&token=xxx 访问，避免加载 watchlist/memo/events
  { path: '/capture/stock/:symbol', element: <CaptureStockPage /> },
  // 受保护路由组
  {
    element: <ProtectedLayout />,
    children: [
      // 普通用户界面（UserAppShell 布局）
      {
        element: <UserAppShell />,
        children: [
          // 需有效订阅的核心业务页面（SubscriberRoute 守卫）
          {
            element: <SubscriberRoute />,
            children: [
              { path: '/market', element: <WatchlistPage /> },
              { path: '/screener', element: <ScreenerPage /> },
              { path: '/stock/:symbol', element: <StockDetailPage /> },
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
              { path: '/admin/strategies', element: <AdminStrategiesPage /> },
              { path: '/admin/jobs', element: <AdminJobsPage /> },
              { path: '/admin/after-close', element: <AdminAfterClosePipelinePage /> },
            ],
          },
        ],
      },
      // 旧路由兼容重定向（保留，避免书签/旧链接 404）
      ...redirectRoutes,
    ],
  },
  // 兜底：未匹配路由重定向到默认入口（替换旧 /overview）
  { path: '*', element: <Navigate to={DEFAULT_ENTRY} replace /> },
])
