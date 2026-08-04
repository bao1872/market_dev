// [Auth] - 描述: 路由配置 + 受保护路由守卫 + Admin/Subscriber 角色守卫
// 公开路由：/（门户页，lazy 加载）, /login, /subscription-expired（canonical），/membership-expired（重定向）
// 受保护路由：认证由 ProtectedLayout 负责（仅校验 auth + access profile，不再固定渲染同一壳层）
// 布局壳拆分（PRD V1.0 阶段一）：
//   UserAppShell   承载普通用户 /market /review /stock/:symbol /messages /settings
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
import { REPLAY_AND_AUCTION_CAPABILITY } from './navigation/capabilities'
import LoginPage from './pages/LoginPage'
import SubscriptionExpiredPage from './pages/SubscriptionExpiredPage'
import MarketWorkspacePage from './features/market-workspace/MarketWorkspacePage'
import BoardAnalysisPage from './pages/BoardAnalysisPage'
import ReviewPage from './pages/ReviewPage'
import StockDetailPage from './pages/StockDetailPage'
import CaptureStockPage from './pages/CaptureStockPage'
import SettingsPage from './pages/SettingsPage'
import MessagesPage from './pages/MessagesPage'
import AdminIndexPage from './pages/AdminIndexPage'
import AdminUsersPage from './pages/AdminUsersPage'
import AdminStockDebugPage from './pages/AdminStockDebugPage'
import AdminDataProductionPage from './pages/AdminDataProductionPage'
import AdminTasksPage from './pages/AdminTasksPage'
import AdminDiagnosticsPage from './pages/AdminDiagnosticsPage'

// 门户页 lazy 加载，避免门户动画代码进入业务页面首包
const LandingPage = lazy(() => import('./pages/LandingPage'))

// [Auction] - 竞价分析三级页面 lazy 加载（市场/板块/个股）
// 受保护路由 require_capability("research_replay")：竞价与复盘同属一项权益（GET /v1/auction/*）
const AuctionMarketPage = lazy(() => import('./features/auction/AuctionMarketPage'))
const AuctionBoardPage = lazy(() => import('./features/auction/AuctionBoardPage'))
const AuctionInstrumentPage = lazy(() => import('./features/auction/AuctionInstrumentPage'))

// 门户页加载占位
function LandingFallback() {
  return <div style={{ minHeight: '100vh', background: '#030915' }} />
}

// [Auction] - 竞价页面 lazy 加载占位（与 UserAppShell 视觉对齐）
function AuctionFallback() {
  return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
}

// 受保护路由布局：仅负责认证与 access profile，不再渲染统一 AppShell
// Capture token 处理已彻底移除：capture=feishu 路由位于 ProtectedLayout 之外，
// 由独立 CaptureStockPage 处理 token。普通受保护路由即使携带 capture 参数也绝不能清除 ACCESS_TOKEN_KEY。
function ProtectedLayout() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const hydrationStatus = useAuthStore((s) => s.hydrationStatus)
  const accessStatus = useAuthStore((s) => s.accessStatus)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)

  // hooks 必须先于任何条件 return（React Hooks 规则）
  const revalidatedRef = useRef(false)
  useEffect(() => {
    if (revalidatedRef.current) return
    revalidatedRef.current = true
    // hydration 完成后且 accessStatus=idle 时触发 /v1/me/access（补水）
    if (accessStatus === 'idle') {
      void revalidateAccess()
    }
  }, [revalidateAccess, accessStatus, hydrationStatus])

  // [权限模型 V2] persist 完成前不渲染受保护路由
  if (hydrationStatus === 'hydrating') {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }

  // 双重检查：zustand isAuthenticated + auth_token（sessionStorage 优先，localStorage 兜底）
  // 防止 token 过期后 isAuthenticated 仍为 true 但 auth_token 已被清除
  const hasToken = !!(
    sessionStorage.getItem(ACCESS_TOKEN_KEY) ?? localStorage.getItem(ACCESS_TOKEN_KEY)
  )
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
// [权限模型 V2] 权限状态机：accessStatus==ready 且确认无权限才跳 /forbidden；
// loading / idle 显示 loading；error 显示"权限加载失败"页（不伪装 403）。
function CapabilityRoute({ capability }: { capability: string }) {
  const user = useAuthStore((s) => s.user)
  const accessStatus = useAuthStore((s) => s.accessStatus)
  const accessError = useAuthStore((s) => s.accessError)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)

  // 权限加载失败：显示失败页 + 重试按钮，不伪装 403
  if (accessStatus === 'error') {
    return (
      <AccessLoadFailedPage
        error={accessError}
        onRetry={() => void revalidateAccess()}
      />
    )
  }

  // 权限未就绪（idle/loading/hydrating）：显示 loading，禁止跳 /forbidden
  if (accessStatus !== 'ready') {
    return <div style={{ minHeight: '100vh', background: '#0A0F14' }} />
  }

  // admin 豁免：所有 capability 默认 active=True
  if (user?.is_admin) {
    return <Outlet />
  }

  // 仅 ready 且后端确认没有所需 capability 才允许跳 /forbidden
  const cap = user?.capabilities?.[capability]
  if (!cap?.active) {
    return <Navigate to="/forbidden" replace />
  }

  return <Outlet />
}

// [Gate2 PRD60] CapabilityAnyRoute - 任一 capability 通过即放行
// 用于 /market 等需要多种权限类型任一即可访问的路由
// [权限模型 V2] 同 CapabilityRoute 状态机：ready 且确认无权限才跳 /forbidden
function CapabilityAnyRoute({ capabilities }: { capabilities: string[] }) {
  const user = useAuthStore((s) => s.user)
  const accessStatus = useAuthStore((s) => s.accessStatus)
  const accessError = useAuthStore((s) => s.accessError)
  const revalidateAccess = useAuthStore((s) => s.revalidateAccess)

  if (accessStatus === 'error') {
    return (
      <AccessLoadFailedPage
        error={accessError}
        onRetry={() => void revalidateAccess()}
      />
    )
  }

  if (accessStatus !== 'ready') {
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

// [权限模型 V2] 权限加载失败页：表达 accessStatus=error（非 403），提供重试
function AccessLoadFailedPage({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
        background: '#0A0F14',
        color: '#E0E0E0',
      }}
    >
      <div>权限加载失败</div>
      <div style={{ color: '#888', fontSize: 13 }}>{error || '无法获取权限上下文，请重试'}</div>
      <button className="btn" onClick={onRetry}>
        重试
      </button>
    </div>
  )
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
              // [CHANGE-20260730-011] 板块分析 V1 页面（任何 market_data 用户可读）
              { path: '/boards', element: <BoardAnalysisPage /> },
              { path: '/boards/:boardId', element: <BoardAnalysisPage /> },
            ],
          },
          // research_replay = 复盘与竞价（CHANGE-20260802-002）
          // 复盘工作台与竞价三级页面共用同一 capability 守卫，不存在独立 auction capability；
          // 直接输入 /auction/* URL 的无权限用户由 CapabilityRoute 统一跳转 /forbidden。
          {
            element: <CapabilityRoute capability={REPLAY_AND_AUCTION_CAPABILITY} />,
            children: [
              { path: '/review', element: <ReviewPage /> },
              {
                path: '/auction',
                element: (
                  <Suspense fallback={<AuctionFallback />}>
                    <AuctionMarketPage />
                  </Suspense>
                ),
              },
              {
                path: '/auction/board/:boardId',
                element: (
                  <Suspense fallback={<AuctionFallback />}>
                    <AuctionBoardPage />
                  </Suspense>
                ),
              },
              {
                path: '/auction/stock/:symbol',
                element: (
                  <Suspense fallback={<AuctionFallback />}>
                    <AuctionInstrumentPage />
                  </Suspense>
                ),
              },
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
              // ===== 目标一级路由（管理后台优化 PRD §6）=====
              { path: '/admin/overview', element: <AdminIndexPage /> },
              // 数据生产中心（盘后流水线并入其中）
              { path: '/admin/data-production', element: <AdminDataProductionPage /> },
              // 任务中心（原"任务与事件"页）
              { path: '/admin/tasks', element: <AdminTasksPage /> },
              // 用户与权限
              { path: '/admin/users', element: <AdminUsersPage /> },
              // 诊断工具（个股调试 + 访问统计并入）
              { path: '/admin/diagnostics', element: <AdminDiagnosticsPage /> },

              // ===== 旧路由兼容重定向（PRD §6.2，保留至少两个正式版本周期）=====
              { path: '/admin', element: <Navigate to="/admin/overview" replace /> },
              { path: '/admin/jobs', element: <Navigate to="/admin/tasks" replace /> },
              {
                path: '/admin/after-close',
                element: <Navigate to="/admin/data-production?tab=after-close" replace />,
              },
              {
                path: '/admin/beta-applications',
                element: <Navigate to="/admin/users?tab=beta-applications" replace />,
              },
              {
                path: '/admin/stocks',
                element: <Navigate to="/admin/diagnostics?tab=stock" replace />,
              },
              {
                path: '/admin/visitors',
                element: <Navigate to="/admin/diagnostics?tab=visitors" replace />,
              },
              // C8: 策略目录页已废弃，重定向到数据生产中心
              { path: '/admin/strategies', element: <Navigate to="/admin/data-production" replace /> },
              // 旧个股调试动态路由：保留（PRD 允许"保留或重定向"），直接渲染调试页
              { path: '/admin/stocks/:symbol/debug', element: <AdminStockDebugPage /> },
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
