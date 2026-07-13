// [AccountMenu] - 描述: 右上角账户菜单（消息 / 设置 / 管理后台 / 返回行情 / 退出）
// 复用现有未读数、用户信息、logout 逻辑；支持点击外部关闭、Escape 关闭、基本 ARIA。
// variant='user'（UserAppShell）：消息 + 设置；admin 额外显示"管理后台"
// variant='admin'（AdminAppShell）：消息 + 设置 + "返回行情"（不重复"管理后台"）
import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import { useUnreadCount } from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { getAccountMenuItemsForVariant, APP_ROUTES, type AccountMenuItem, type AccountMenuVariant } from '@/navigation/appNavigation'
import styles from './AccountMenu.module.scss'

// [AccountMenu] - 描述: 消息路由常量（用于动态化消息项链接）
const APP_ROUTES_MESSAGES = APP_ROUTES.messages

// displayName 优先 user.name，其次 user.email，最后通用"用户"
function getDisplayName(name?: string, email?: string): string {
  return name || email || '用户'
}

// initials 优先 name 首字母，其次 email 用户名首字母，最后通用字符
function getInitials(name?: string, email?: string): string {
  if (name) {
    const initials = name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
    if (initials) return initials
  }
  if (email) {
    const username = email.split('@')[0]
    if (username) return username.slice(0, 2).toUpperCase()
  }
  return 'U'
}

interface AccountMenuProps {
  /** 'user' = UserAppShell 上下文（消息+设置；admin 额外显示管理后台）；'admin' = AdminAppShell 上下文（显示返回行情） */
  variant?: AccountMenuVariant
}

export default function AccountMenu({ variant = 'user' }: AccountMenuProps) {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  // 真实未读消息数（角标专用接口，与 AppShell 旧实现一致）
  const unreadQuery = useUnreadCount()
  const unread = unreadQuery.data?.unread_count ?? 0

  const isAdmin = user?.is_admin === true
  const displayName = getDisplayName(user?.name, user?.email)
  const initials = getInitials(user?.name, user?.email)

  // 构建菜单项：复用 appNavigation 单一真源（消息 + 设置 + variant 决定的第三项）
  const items: AccountMenuItem[] = getAccountMenuItemsForVariant(isAdmin, variant)

  // 点击外部 / Escape 关闭
  useEffect(() => {
    if (!open) return
    function onPointerDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  function handleLogout() {
    logout()
    toast.show('已退出登录', '期待下次相见')
    navigate('/login')
  }

  return (
    <div className={styles.wrap} ref={wrapRef}>
      <button
        type="button"
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="账户菜单"
        onClick={() => setOpen((v) => !v)}
      >
        <span className={styles.avatar}>{initials}</span>
        {unread > 0 && (
          <span className={styles.badge} aria-hidden>
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div className={styles.menu} role="menu">
          <div className={styles.head}>
            <div className={styles.name}>{displayName}</div>
            {user?.email && <div className={styles.email}>{user?.email}</div>}
          </div>
          {items.map((item: AccountMenuItem) => {
            // [AccountMenu] - 描述: 消息项动态化 — unread>0 时进入 /messages?filter=unread 并显示未读数
            const isMessages = item.path === APP_ROUTES_MESSAGES
            const linkPath = isMessages && unread > 0 ? `${item.path}?filter=unread` : item.path
            return (
              <Link
                key={item.path}
                to={linkPath}
                role="menuitem"
                className={styles.item}
                onClick={() => setOpen(false)}
              >
                <span>{item.label}</span>
                {isMessages && unread > 0 && (
                  <span className={styles.itemBadge} aria-hidden>
                    {unread > 99 ? '99+' : unread}
                  </span>
                )}
              </Link>
            )
          })}
          <button type="button" role="menuitem" className={styles.item} onClick={handleLogout}>
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}
