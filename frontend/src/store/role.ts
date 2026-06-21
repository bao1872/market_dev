// 角色导航 store（V1.5.1：默认管理员，可切换普通用户视图）
// 对应原型 app.js initRoleNavigation()
// 管理员页面始终保持管理员角色，避免从后台误切换后失去导航
import { create } from 'zustand'

export type UserRole = 'admin' | 'user'

interface RoleState {
  role: UserRole
  isAdmin: boolean
  setRole: (role: UserRole) => void
  toggleRole: () => void
}

function getInitialRole(): UserRole {
  const params = new URLSearchParams(window.location.search)
  const adminPath = window.location.pathname.includes('/admin/')
  // 管理员页面始终保持管理员角色
  if (adminPath) return 'admin'
  return params.get('role') === 'user' ? 'user' : 'admin'
}

export const useRoleStore = create<RoleState>((set, get) => ({
  role: getInitialRole(),
  get isAdmin() {
    return get().role !== 'user'
  },
  setRole: (role) => set({ role }),
  toggleRole: () => {
    const current = get().role
    const next: UserRole = current === 'admin' ? 'user' : 'admin'
    // 更新 URL 参数
    const url = new URL(window.location.href)
    if (next === 'user') {
      url.searchParams.set('role', 'user')
    } else {
      url.searchParams.delete('role')
    }
    window.location.href = url.toString()
  },
}))
