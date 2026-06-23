// AppShell：应用主布局壳（侧栏 + 顶栏 + 内容区 + Toast）
// 对应原型：所有页面的 .app-shell > .sidebar + .topbar + .main 结构
// V1.5.1：角色感知导航，管理员页面始终显示管理员导航
import { type ReactNode, useState, useEffect } from 'react'
import { useLocation, Link } from 'react-router-dom'
import { useRoleStore } from '@/store/role'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'
import { getMarketStatus, type MarketStatus } from '@/api/endpoints'
import clsx from 'clsx'

// 导航项定义
interface NavItemDef {
  path: string
  icon: string
  label: string
  badge?: string
  badgeHot?: boolean
}

const userNavItems: NavItemDef[] = [
  { path: '/', icon: '⌂', label: '服务总览' },
  { path: '/screener', icon: '⌁', label: '选股策略' },
  { path: '/watchlist', icon: '☆', label: '我的自选' },
  { path: '/messages', icon: '◉', label: '消息中心', badge: '7', badgeHot: true },
  { path: '/settings', icon: '⚙', label: '通知与设置' },
]

const adminNavItems: NavItemDef[] = [
  { path: '/admin', icon: '▦', label: '系统概览' },
  { path: '/admin/users', icon: '♙', label: '用户与套餐' },
  { path: '/admin/strategies', icon: '◇', label: '策略目录' },
  { path: '/admin/jobs', icon: '↻', label: '任务与事件' },
]

// 页面标题映射
const pageTitleMap: Record<string, string> = {
  '/': '服务总览',
  '/screener': '选股策略',
  '/watchlist': '我的自选',
  '/monitoring-plan-editor': '监控方案编辑',
  '/settings': '通知与设置',
  '/messages': '消息中心',
  '/admin': '系统概览',
  '/admin/users': '用户与套餐',
  '/admin/strategies': '策略目录',
  '/admin/jobs': '任务与事件',
}

function getPageTitle(pathname: string): string {
  // 个股详情页特殊处理
  if (pathname.startsWith('/stock/')) return '个股详情'
  return pageTitleMap[pathname] || '量策服务台'
}

function NavItem({ item, active }: { item: NavItemDef; active: boolean }) {
  return (
    <Link
      to={item.path}
      className={clsx('nav-item', active && 'active')}
    >
      <span className="nav-icon">{item.icon}</span>
      <span>{item.label}</span>
      {item.badge && (
        <span className={clsx('nav-badge', item.badgeHot && 'hot')}>{item.badge}</span>
      )}
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

  // 市场状态轮询（30s）
  const [marketStatus, setMarketStatus] = useState<MarketStatus | null>(null)
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const status = await getMarketStatus()
        setMarketStatus(status)
      } catch {
        // API 失败时保持当前状态
      }
    }
    fetchStatus()
    const interval = setInterval(fetchStatus, 30000)
    return () => clearInterval(interval)
  }, [])

  // 实时时钟（1s 刷新）
  const [currentTime, setCurrentTime] = useState(
    new Date().toLocaleTimeString('zh-CN', { hour12: false })
  )
  useEffect(() => {
    const interval = setInterval(() => {
      setCurrentTime(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    }, 1000)
    return () => clearInterval(interval)
  }, [])

  // 获取用户名首字母作为头像
  const userInitials = user?.name
    ? user.name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
    : 'DL'
  const userName = user?.name || 'dan lu'
  const userRoleLabel = isAdmin ? '超级管理员 · 测试账号' : '普通用户预览'

  return (
    <div className="app-shell">
      {/* 侧栏 */}
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">QS</div>
          <div>
            <div className="brand-title">量策服务台</div>
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
            <div className="system-mini-row">
              <span>
                <i className="dot ok"></i>分钟行情
              </span>
              <span>实时</span>
            </div>
            <div className="system-mini-row">
              <span>
                <i className="dot ok"></i>策略引擎
              </span>
              <span>正常</span>
            </div>
            <div className="system-mini-row">
              <span>
                <i className="dot ok"></i>消息队列
              </span>
              <span>3</span>
            </div>
          </div>
        </div>
      </aside>

      {/* 顶栏 */}
      <header className="topbar">
        <div className="top-left">
          <div className="page-crumb">{pageTitle}</div>
          <div className="top-status">
            <i className={marketStatus?.is_trading_hours ? 'dot ok' : 'dot'}></i>
            A股{marketStatus?.status_text ?? '加载中'} · {currentTime}
          </div>
        </div>
        <div className="top-right">
          <div className="search-wrap">
            <input
              className="search-global"
              placeholder="搜索股票代码 / 名称"
            />
          </div>
          <button className="icon-btn" title="系统通知">
            ◔
          </button>
          {/* V1.5.1：角色感知导航切换按钮 */}
          {!isAdminPath && (
            <button
              className="btn small role-preview-toggle"
              onClick={toggleRole}
              title={isAdmin ? '仅用于检查普通用户权限界面' : '恢复默认管理员测试账号'}
            >
              {isAdmin ? '切换普通用户视图' : '返回管理员视图'}
            </button>
          )}
          <div className="avatar">{userInitials}</div>
          <div>
            <div style={{ fontSize: '11px', fontWeight: 650 }}>{userName}</div>
            <div style={{ fontSize: '9px', color: 'var(--muted)' }}>{userRoleLabel}</div>
          </div>
        </div>
      </header>

      {/* 主内容区 */}
      <main className="main">
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
