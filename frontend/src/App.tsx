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
import AdminVisitorsPage from './pages/AdminVisitorsPage'

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

// [Phase 5B-2 PRD60 PA-01] 旧 SubscriberRoute 已被 CapabilityRoute 替代
// 三类独立 capability 守卫已替代统一订阅检查（self_selection/market_data/research_replay）

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

// [Phase 5B-2 PRD60 PA-01] CapabilityRoute - 三类独立权限守卫
// capability: 'self_selection' | 'market_data' | 'research_replay'
// admin 豁免；accessLoading 期间显示 loading（等待 /me/access 返回 capabilities）
// 无权限时跳转到 /forbidden 页面（区分于 /subscription-expired 的"订阅过期"语义）
function CapabilityRoute({ capability }: { capability: string }) {
  const user = useAuthStore((s) => s.user)
  const accessLoading = useAuthStore((s) => s.accessLoading)

  // 仅在 capabilities 尚未加载时显示 loading（首次登录或刷新后无持久化状态）
  // 已有持久化 capabilities 时不阻塞渲染，避免页面重载时 loading 闪烁导致 E2E 选择器失配；
  // revalidateAccess 仍会异步刷新权限，若过期后会在 /me/access 响应后跳转 /subscription-expired
  const hasCapability = !!user?.capabilities?.[capability]
  if (accessLoading && !hasCapability) {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }

  // admin 豁免：所有 capability 默认 active=True
  if (user?.is_admin) {
    return <Outlet />
  }

  // 检查 capability 是否存在且 active
  const cap = user?.capabilities?.[capability]
  if (!cap?.active) {
    return <Navigate to="/forbidden" replace />
  }

  return <Outlet />
}

// [Gate2 PRD60] CapabilityAnyRoute - 任一 capability 通过即放行
// 用于 /market 等需要多种权限类型任一即可访问的路由
// 如 /market 允许 self_selection（自选管理）或 market_data（行情管理）任一进入
function CapabilityAnyRoute({ capabilities }: { capabilities: string[] }) {
  const user = useAuthStore((s) => s.user)
  const accessLoading = useAuthStore((s) => s.accessLoading)

  const hasAny = capabilities.some((cap) => !!user?.capabilities?.[cap])
  if (accessLoading && !hasAny) {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }

  if (user?.is_admin) {
    return <Outlet />
  }

  const hasActive = capabilities.some((cap) => {
    const c = user?.capabilities?.[cap]
    return c?.active
  })
  if (!hasActive) {
    return <Navigate to="/forbidden" replace />
  }

  return <Outlet />
}

// [Phase 5B-2 PRD60 PA-01] 简易 403 页面 - 用户已登录但缺少指定 capability
function ForbiddenPage() {
  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0A0F14', color: '#E0E0E0' }}>
      <div style={{ textAlign: 'center' }}>
        <h1 style={{ fontSize: '48px', margin: '0 0 16px' }}>403</h1>
        <p style={{ fontSize: '16px', opacity: 0.7 }}>权限不足，当前账号未开通此功能</p>
      </div>
    </div>
  )
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
  // [Phase 5B-2 PRD60 PA-01] 403 页面 - 已登录但缺少指定 capability
  { path: '/forbidden', element: <ForbiddenPage /> },
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
          // [Gate2 PRD60] /market 允许 self_selection 或 market_data 任一进入
          // 仅 self_selection 用户可看行情列表+自选+盘中，但详情按钮禁用（API 403）
          // 仅 market_data 用户可看行情+详情，但隐藏自选/盘中入口
          {
            element: <CapabilityAnyRoute capabilities={['self_selection', 'market_data']} />,
            children: [
              { path: '/market', element: <MarketWorkspacePage /> },
            ],
          },
          // market_data: 行情数据+个股详情（/stock/:symbol）
          {
            element: <CapabilityRoute capability="market_data" />,
            children: [
              { path: '/stock/:symbol', element: <StockDetailPage /> },
            ],
          },
          // research_replay: 复盘入口（/replay）
          {
            element: <CapabilityRoute capability="research_replay" />,
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
              { path: '/admin/visitors', element: <AdminVisitorsPage /> },
            ],
          },
        ],
      },
      // 旧路由兼容重定向（保留，避免书签/旧链接 404）
      ...redirectRoutes,
      // [Phase4] 旧管理员调试动态路由重定向
      { path: '/admin/stock-debug/:symbol', element: <OldStockDebugRedirect /> },
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
