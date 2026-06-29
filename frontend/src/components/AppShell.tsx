// AppShell：应用主布局壳（侧栏 + 顶栏 + 内容区 + Toast）
// 对应原型：所有页面的 .app-shell > .sidebar + .topbar + .main 结构
// V1.5.1：角色感知导航，管理员页面始终显示管理员导航
// V1.6.3：capture=feishu 参数触发截图模式，隐藏侧栏、用户信息与角色切换按钮
// V1.6.4：移动端汉堡按钮、抽屉侧栏、真实未读数与健康状态
import { type ReactNode, useState, useEffect } from 'react'
import { useLocation, Link } from 'react-router-dom'
import { useRoleStore } from '@/store/role'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'
import { getMarketStatus, type MarketStatus } from '@/api/endpoints'
import { useUnreadCount, useHealth, useAdminSystemOverview, setCachedMarketStatus } from '@/hooks/useApi'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import BrandLogo from '@/components/BrandLogo'
import clsx from 'clsx'

// 导航项定义
interface NavItemDef {
  path: string
  icon: string
  label: string
}

const userNavItems: NavItemDef[] = [
  { path: '/overview', icon: '⌂', label: '主页' },
  { path: '/screener', icon: '⌁', label: '趋势选股' },
  { path: '/watchlist', icon: '☆', label: '我的自选' },
  { path: '/messages', icon: '◉', label: '消息中心' },
  { path: '/settings', icon: '⚙', label: '通知与设置' },
]

const adminNavItems: NavItemDef[] = [
  { path: '/admin', icon: '▦', label: '系统概览' },
  { path: '/admin/users', icon: '♙', label: '用户与套餐' },
  { path: '/admin/beta-applications', icon: '✦', label: '内测申请' },
  { path: '/admin/strategies', icon: '◇', label: '策略目录' },
  { path: '/admin/jobs', icon: '↻', label: '任务与事件' },
]

// 页面标题映射
const pageTitleMap: Record<string, string> = {
  '/overview': '主页',
  '/screener': '趋势选股',
  '/watchlist': '我的自选',
  '/settings': '通知与设置',
  '/messages': '消息中心',
  '/admin': '系统概览',
  '/admin/users': '用户与套餐',
  '/admin/beta-applications': '内测申请',
  '/admin/strategies': '策略目录',
  '/admin/jobs': '任务与事件',
}

function getPageTitle(pathname: string): string {
  // 个股详情页特殊处理
  if (pathname.startsWith('/stock/')) return '个股详情'
  return pageTitleMap[pathname] || '盘迹'
}

function NavItem({ item, active, onClick }: { item: NavItemDef; active: boolean; onClick?: () => void }) {
  return (
    <Link
      to={item.path}
      className={clsx('nav-item', active && 'active')}
      onClick={onClick}
    >
      <span className="nav-icon">{item.icon}</span>
      <span>{item.label}</span>
    </Link>
  )
}

export default function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation()
  const { isAdmin, toggleRole } = useRoleStore()
  const { user } = useAuthStore()
  const toast = useToast()

  const currentPath = location.pathname
  const pageTitle = getPageTitle(currentPath)
  const isAdminPath = currentPath.startsWith('/admin')

  // 截图模式：URL 参数 capture=feishu 时隐藏侧栏、用户信息与角色切换
  const isCaptureMode = new URLSearchParams(location.search).get('capture') === 'feishu'

  // 移动端抽屉状态
  const [drawerOpen, setDrawerOpen] = useState(false)

  // 路由切换后自动关闭抽屉
  useEffect(() => {
    setDrawerOpen(false)
  }, [currentPath])

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
  const [currentTime, setCurrentTime] = useState(
    formatShanghaiTimeShort(new Date()),
  )
  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentTime(formatShanghaiTimeShort(new Date()))
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  // [Messages] - 描述: 真实未读消息数（角标专用接口，避免 list 接口 total 字段语义混淆）
  const unreadCountQuery = useUnreadCount()
  const unreadCount = unreadCountQuery.data?.unread_count ?? 0

  // 真实后端健康状态
  const healthQuery = useHealth()
  const isServiceHealthy = healthQuery.data?.status === 'ok'

  // 管理员系统概览（详细状态）- [AppShell] - 描述: 仅 admin 角色启用，避免普通用户触发 403
  const adminOverviewQuery = useAdminSystemOverview(!!user && user.role === 'admin')
  const adminOverview = adminOverviewQuery.data

  // 获取用户名首字母作为头像
  const userInitials = user?.name
    ? user.name
        .split(' ')
        .map((w) => w[0])
        .join('')
        .slice(0, 2)
        .toUpperCase()
    : 'DL'
  const userName = user?.name || 'dan lu'
  const userEmail = user?.email || ''
  const userRoleLabel = isAdmin ? '超级管理员 · 测试账号' : '普通用户预览'

  // 侧栏导航内容
  const sidebarContent = (
    <>
      <div className="brand">
        <BrandLogo variant="sidebar" />
        <div>
          <div className="brand-title">盘迹</div>
          <div className="brand-sub">STRATEGY SERVICE</div>
        </div>
      </div>

      {/* 用户服务导航 */}
      <div className="nav-section">
        <div className="nav-label">用户服务</div>
        {userNavItems.map((item) => (
          <NavItem
            key={item.path}
            item={item}
            active={currentPath === item.path}
          />
        ))}
      </div>

      {/* 管理员控制台导航（V1.5.1：默认显示，普通用户视图隐藏） */}
      {isAdmin && (
        <div className="nav-section">
          <div className="nav-label">管理员控制台</div>
          {adminNavItems.map((item) => (
            <NavItem
              key={item.path}
              item={item}
              active={currentPath === item.path}
            />
          ))}
        </div>
      )}

      {/* 侧栏底部系统状态 */}
      <div className="sidebar-footer">
        <div className="system-mini">
          {/* 普通用户：简化服务状态 */}
          {!isAdmin && (
            <div className="system-mini-row">
              <span>
                <i className={clsx('dot', isServiceHealthy ? 'ok' : 'err')}></i>服务状态
              </span>
              <span>{healthQuery.isLoading ? '检测中' : isServiceHealthy ? '正常' : '异常'}</span>
            </div>
          )}

          {/* 管理员：详细 Worker / 调度器 / 队列状态 */}
          {isAdmin && (
            <>
              <div className="system-mini-row">
                <span>
                  <i
                    className={clsx(
                      'dot',
                      adminOverview?.worker_health === 'healthy' ? 'ok' : 'warn',
                    )}
                  ></i>
                  策略引擎
                </span>
                <span>{adminOverview ? (adminOverview.worker_health === 'healthy' ? '正常' : '降级') : '加载中'}</span>
              </div>
              <div className="system-mini-row">
                <span>
                  <i
                    className={clsx(
                      'dot',
                      adminOverview?.scheduler_health === 'healthy' ? 'ok' : 'warn',
                    )}
                  ></i>
                  任务调度
                </span>
                <span>{adminOverview ? (adminOverview.scheduler_health === 'healthy' ? '正常' : '降级') : '加载中'}</span>
              </div>
              <div className="system-mini-row">
                <span>
                  <i
                    className={clsx(
                      'dot',
                      (adminOverview?.queue_backlog ?? 0) < 10 ? 'ok' : 'warn',
                    )}
                  ></i>
                  消息队列
                </span>
                <span>{adminOverview?.queue_backlog ?? '-'}</span>
              </div>
            </>
          )}
        </div>
      </div>
    </>
  )

  return (
    <div className={clsx('app-shell', isCaptureMode && 'capture-mode')}>
      {/* 侧栏（桌面端固定显示） */}
      {!isCaptureMode && (
        <aside className="sidebar sidebar-desktop">{sidebarContent}</aside>
      )}

      {/* 移动端抽屉侧栏 + 遮罩 */}
      {!isCaptureMode && drawerOpen && (
        <>
          <div className="sidebar-overlay open" onClick={() => setDrawerOpen(false)} />
          <aside className="sidebar sidebar-drawer open">
            <div className="sidebar-drawer-head">
              <div className="brand">
                <BrandLogo variant="sidebar" />
                <div>
                  <div className="brand-title">盘迹</div>
                  <div className="brand-sub">STRATEGY SERVICE</div>
                </div>
              </div>
              <button
                className="icon-btn drawer-close"
                onClick={() => setDrawerOpen(false)}
                aria-label="关闭导航"
              >
                ×
              </button>
            </div>
            <div className="sidebar-drawer-body">{sidebarContent}</div>
          </aside>
        </>
      )}

      {/* 顶栏 */}
      <header className="topbar">
        <div className="top-left">
          {/* 移动端汉堡按钮 */}
          {!isCaptureMode && (
            <button
              className="icon-btn hamburger"
              onClick={() => setDrawerOpen((prev) => !prev)}
              aria-label="打开导航"
            >
              ☰
            </button>
          )}
          <div className="page-crumb">{pageTitle}</div>
          <div className="top-status">
            <i className={marketStatus?.is_trading_hours ? 'dot ok' : 'dot'}></i>
            A股{marketStatus?.status_text ?? '加载中'} · {currentTime}
          </div>
        </div>
        <div className="top-right">
          <Link className="icon-btn messages-btn" to="/messages?filter=unread" title="消息中心">
            ◔
            {unreadCount > 0 && <span className="messages-badge">{unreadCount > 99 ? '99+' : unreadCount}</span>}
          </Link>
          {/* V1.5.1：角色感知导航切换按钮（截图模式隐藏） */}
          {!isCaptureMode && !isAdminPath && (
            <button
              className="btn small role-preview-toggle"
              onClick={toggleRole}
              title={isAdmin ? '仅用于检查普通用户权限界面' : '恢复默认管理员测试账号'}
            >
              {isAdmin ? '切换普通用户视图' : '返回管理员视图'}
            </button>
          )}
          {!isCaptureMode && (
            <>
              <div className="avatar">{userInitials}</div>
              <div className="user-info">
                <div className="user-name">{userName}</div>
                <div className="user-role">{userRoleLabel}</div>
                {userEmail && <div className="user-email">{userEmail}</div>}
              </div>
            </>
          )}
        </div>
      </header>

      {/* 主内容区 */}
      <main className="main" style={isCaptureMode ? { marginLeft: 0 } : undefined}>
        <div className="content">{children}</div>
      </main>

      {/* Toast 通知 */}
      {toast.visible && (
        <div className="toast show">
          <div className="toast-title">{toast.title}</div>
          <div className="toast-msg">{toast.message}</div>
        </div>
      )}
    </div>
  )
}
