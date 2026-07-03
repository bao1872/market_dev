// [Auth] - 描述: 路由配置 + 受保护路由守卫 + Admin/Subscriber 角色守卫
// 公开路由：/（门户页，lazy 加载）, /login, /subscription-expired（canonical），/membership-expired（重定向）
// 受保护路由：其余所有路由（通过 ProtectedLayout 校验 auth store + AppShell 布局）
// SubscriberRoute：有效订阅或 admin 豁免，否则重定向到 /subscription-expired
// AdminRoute：is_admin=true 才可访问，否则重定向到 /overview
import { lazy, Suspense, useEffect, useRef } from 'react'
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import { useAuthStore, ACCESS_TOKEN_KEY, CAPTURE_TOKEN_KEY } from './store/auth'
import AppShell from './components/AppShell'
import LoginPage from './pages/LoginPage'
import SubscriptionExpiredPage from './pages/SubscriptionExpiredPage'
import IndexPage from './pages/IndexPage'
import ScreenerPage from './pages/ScreenerPage'
import WatchlistPage from './pages/WatchlistPage'
import StockDetailPage from './pages/StockDetailPage'
import CaptureStockPage from './pages/CaptureStockPage'
import SettingsPage from './pages/SettingsPage'
import MessagesPage from './pages/MessagesPage'
import AdminIndexPage from './pages/AdminIndexPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminStrategiesPage from './pages/AdminStrategiesPage'
import AdminJobsPage from './pages/AdminJobsPage'
import AdminBetaApplicationsPage from './pages/AdminBetaApplicationsPage'

// 门户页 lazy 加载，避免门户动画代码进入业务页面首包
const LandingPage = lazy(() => import('./pages/LandingPage'))

// 门户页加载占位
function LandingFallback() {
  return <div style={{ minHeight: '100vh', background: '#030915' }} />
}

// 受保护路由布局：未登录或 token 缺失重定向到 /login；已登录用 AppShell 包裹
function ProtectedLayout() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)
  const location = window.location
  const searchParams = new URLSearchParams(location.search)
  // 截图模式：URL 带 capture=feishu 且 token 有效时，允许直接访问
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
  // capture 模式使用 URL token + capture_token key，不依赖 auth_token storage
  const hasToken = !!localStorage.getItem(ACCESS_TOKEN_KEY)
  if (!isAuthenticated || !hasToken) {
    // 截图模式放行（capture token 通过 URL + capture_token key 提供，apiClient 拦截器读取）
    if (isCaptureMode && captureToken) {
      return (
        <AppShell>
          <Outlet />
        </AppShell>
      )
    }
    return <Navigate to="/login" replace />
  }
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  )
}

// [Auth] - 描述: SubscriberRoute 订阅守卫 - 非有效订阅用户重定向到 /subscription-expired（canonical）
// admin 用户豁免（is_admin=true 直接通过，不强制订阅）
// 用于 /overview /screener /watchlist 等需有效订阅的核心业务路由
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
// 非 admin 用户重定向到 /overview
function AdminRoute() {
  const user = useAuthStore((s) => s.user)
  if (user?.is_admin !== true) {
    return <Navigate to="/overview" replace />
  }
  return <Outlet />
}

export const router = createBrowserRouter([
  // 公开路由
  { path: '/', element: <Suspense fallback={<LandingFallback />}><LandingPage /></Suspense> },
  { path: '/login', element: <LoginPage /> },
  // [Auth] - 描述: /subscription-expired 为 canonical 路由，/membership-expired 重定向到此（向后兼容）
  { path: '/subscription-expired', element: <SubscriptionExpiredPage /> },
  { path: '/membership-expired', element: <Navigate to="/subscription-expired" replace /> },
  // [capture-mode] 专用 Capture 路由（不经过 ProtectedLayout/SubscriberRoute/AppShell，只使用 captureClient）
  // capture worker 通过 /capture/stock/:symbol?capture=feishu&token=xxx 访问，避免加载 watchlist/memo/events
  { path: '/capture/stock/:symbol', element: <CaptureStockPage /> },
  // 受保护路由组
  {
    element: <ProtectedLayout />,
    children: [
      // 需有效订阅的核心业务页面（SubscriberRoute 守卫）
      {
        element: <SubscriberRoute />,
        children: [
          { path: '/overview', element: <IndexPage /> },
          { path: '/screener', element: <ScreenerPage /> },
          { path: '/watchlist', element: <WatchlistPage /> },
          { path: '/stock/:symbol', element: <StockDetailPage /> },
        ],
      },
      // 不强制订阅的辅助页面（仅认证即可）
      { path: '/settings', element: <SettingsPage /> },
      { path: '/messages', element: <MessagesPage /> },
      // Admin 页面（额外角色守卫）
      {
        element: <AdminRoute />,
        children: [
          // [Auth] - 描述: /admin/overview 为后端 next_route 返回值，与 /admin 同渲染 AdminIndexPage
          { path: '/admin', element: <AdminIndexPage /> },
          { path: '/admin/overview', element: <AdminIndexPage /> },
          { path: '/admin/users', element: <AdminUsersPage /> },
          { path: '/admin/beta-applications', element: <AdminBetaApplicationsPage /> },
          { path: '/admin/strategies', element: <AdminStrategiesPage /> },
          { path: '/admin/jobs', element: <AdminJobsPage /> },
        ],
      },
    ],
  },
  // 兜底：未匹配路由重定向到主页（保留原"未匹配进服务台"语义）
  { path: '*', element: <Navigate to="/overview" replace /> },
])
