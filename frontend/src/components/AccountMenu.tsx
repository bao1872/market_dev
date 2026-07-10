// [AccountMenu] - 描述: 右上角账户菜单（消息 / 设置 / 管理后台 / 退出）
// 复用现有未读数、用户信息、logout 逻辑；支持点击外部关闭、Escape 关闭、基本 ARIA。
// 删除原普通用户左侧栏中的消息/设置入口后，二者统一收拢到此处。
import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import { useUnreadCount } from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { getAccountMenuItems, type AccountMenuItem } from '@/navigation/appNavigation'
import styles from './AccountMenu.module.scss'

// 由用户名抽取首字母作为头像（与 AuthUser.name = email 约定一致）
function getInitials(name?: string): string {
  if (!name) return 'DL'
  return name
    .split(' ')
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

export default function AccountMenu() {
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
  const items = getAccountMenuItems(isAdmin)

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
        <span className={styles.avatar}>{getInitials(user?.name)}</span>
        {unread > 0 && (
          <span className={styles.badge} aria-hidden>
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div className={styles.menu} role="menu">
          <div className={styles.head}>
            <div className={styles.name}>{user?.name || 'dan lu'}</div>
            {user?.email && <div className={styles.email}>{user?.email}</div>}
          </div>
          {items.map((item: AccountMenuItem) => (
            <Link
              key={item.path}
              to={item.path}
              role="menuitem"
              className={styles.item}
              onClick={() => setOpen(false)}
            >
              {item.label}
            </Link>
          ))}
          <button type="button" role="menuitem" className={styles.item} onClick={handleLogout}>
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}
