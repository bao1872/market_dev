// 路由配置：createBrowserRouter + 受保护路由守卫 + Admin 角色守卫
// 公开路由：/login, /membership-expired
// 受保护路由：其余所有路由（通过 ProtectedLayout 校验 auth store + AppShell 布局）
// Admin 路由：额外通过 AdminRoute 校验 role === 'admin'
import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'
import { useAuthStore } from './store/auth'
import AppShell from './components/AppShell'
import LoginPage from './pages/LoginPage'
import MembershipExpiredPage from './pages/MembershipExpiredPage'
import IndexPage from './pages/IndexPage'
import ScreenerPage from './pages/ScreenerPage'
import StrategyPlanEditorPage from './pages/StrategyPlanEditorPage'
import WatchlistPage from './pages/WatchlistPage'
import MonitoringPlanEditorPage from './pages/MonitoringPlanEditorPage'
import StockDetailPage from './pages/StockDetailPage'
import SettingsPage from './pages/SettingsPage'
import MessagesPage from './pages/MessagesPage'
import AdminIndexPage from './pages/AdminIndexPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminStrategiesPage from './pages/AdminStrategiesPage'
import AdminConfigPage from './pages/AdminConfigPage'
import AdminJobsPage from './pages/AdminJobsPage'

// 受保护路由布局：未登录重定向到 /login；已登录用 AppShell 包裹
function ProtectedLayout() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  )
}

// Admin 角色守卫：非 admin 用户重定向到首页
function AdminRoute() {
  const user = useAuthStore((s) => s.user)
  if (user?.role !== 'admin') {
    return <Navigate to="/" replace />
  }
  return <Outlet />
}

export const router = createBrowserRouter([
  // 公开路由
  { path: '/login', element: <LoginPage /> },
  { path: '/membership-expired', element: <MembershipExpiredPage /> },
  // 受保护路由组
  {
    element: <ProtectedLayout />,
    children: [
      // 用户页面
      { path: '/', element: <IndexPage /> },
      { path: '/screener', element: <ScreenerPage /> },
      { path: '/strategy-plan-editor', element: <StrategyPlanEditorPage /> },
      { path: '/watchlist', element: <WatchlistPage /> },
      { path: '/monitoring-plan-editor', element: <MonitoringPlanEditorPage /> },
      { path: '/stock/:symbol', element: <StockDetailPage /> },
      { path: '/settings', element: <SettingsPage /> },
      { path: '/messages', element: <MessagesPage /> },
      // Admin 页面（额外角色守卫）
      {
        element: <AdminRoute />,
        children: [
          { path: '/admin', element: <AdminIndexPage /> },
          { path: '/admin/users', element: <AdminUsersPage /> },
          { path: '/admin/strategies', element: <AdminStrategiesPage /> },
          { path: '/admin/config', element: <AdminConfigPage /> },
          { path: '/admin/jobs', element: <AdminJobsPage /> },
        ],
      },
    ],
  },
  // 兜底：未匹配路由重定向到首页
  { path: '*', element: <Navigate to="/" replace /> },
])
