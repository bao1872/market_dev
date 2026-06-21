// 认证状态 store（zustand + persist 持久化）
// 管理登录态、当前用户、token；登录态用于路由守卫
// persist 中间件将状态持久化到 localStorage，页面刷新后保持登录态
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type UserRole = 'admin' | 'member'

export interface AuthUser {
  id: string
  name: string
  email: string
  role: UserRole
}

interface AuthState {
  isAuthenticated: boolean
  user: AuthUser | null
  token: string | null
  login: (token: string, user: AuthUser) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      isAuthenticated: false,
      user: null,
      token: null,
      login: (token, user) => {
        localStorage.setItem('auth_token', token)
        set({ isAuthenticated: true, token, user })
      },
      logout: () => {
        localStorage.removeItem('auth_token')
        set({ isAuthenticated: false, token: null, user: null })
      },
    }),
    {
      name: 'auth-store',
      // 只持久化 isAuthenticated、user、token（不持久化 login/logout 方法）
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        user: state.user,
        token: state.token,
      }),
    },
  ),
)
