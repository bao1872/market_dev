// 认证状态 store（zustand + persist 自定义持久化）
// 管理登录态、当前用户、access_token + refresh_token
// 持久化策略（"保持登录"开关）：
//   - keepLogin=true  → localStorage（关闭浏览器后保留登录态）
//   - keepLogin=false → sessionStorage（关闭标签页后清除，更安全）
// token 同步写入 auth_token / auth_refresh_token key，供 client.ts 拦截器直接读取
// （避免 store 初始化时序问题，且兼容 capture 模式写 localStorage 的场景）
import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'

// [Auth] - 描述: AuthUser 当前用户身份 + AccessProfile 权限上下文（对齐后端 LoginResponse 字段）
// 替代旧 role: 'admin' | 'member' 单值，改用 is_admin + roles[] + subscription_active 等
// 唯一真源为后端 get_access_context，前端不在本地计算权限
export interface AuthUser {
  id: string
  name: string  // = email（兼容 AppShell 头像首字母抽取）
  email: string
  is_admin: boolean
  roles: string[]
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
}

// token 在 storage 中的 key（client.ts 拦截器读取这两个 key）
export const ACCESS_TOKEN_KEY = 'auth_token'
export const REFRESH_TOKEN_KEY = 'auth_refresh_token'

// 当前会话的存储选择标志：login 时设置，决定 persist 写入哪个 storage
// 模块级变量，默认 true（保持登录）；onRehydrateStorage 恢复时同步为 state.keepLogin
let _keepLogin = true

// 自定义 storage：根据 _keepLogin 选择 localStorage 或 sessionStorage
// setItem 先清对方 storage，避免 keepLogin 切换后旧数据残留（保证唯一存储位置）
// getItem 优先读 sessionStorage（未保持登录的当前会话），再 localStorage（保持登录或 capture 模式）
const dynamicStorage: StateStorage = {
  getItem: (name) => sessionStorage.getItem(name) ?? localStorage.getItem(name),
  setItem: (name, value) => {
    sessionStorage.removeItem(name)
    localStorage.removeItem(name)
    if (_keepLogin) localStorage.setItem(name, value)
    else sessionStorage.setItem(name, value)
  },
  removeItem: (name) => {
    sessionStorage.removeItem(name)
    localStorage.removeItem(name)
  },
}

// 写入 token 对到当前 keepLogin 对应的 storage（先清两个 storage 避免残留）
function writeTokenPair(accessToken: string, refreshToken: string): void {
  sessionStorage.removeItem(ACCESS_TOKEN_KEY)
  sessionStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  if (_keepLogin) {
    localStorage.setItem(ACCESS_TOKEN_KEY, accessToken)
    localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
  } else {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, accessToken)
    sessionStorage.setItem(REFRESH_TOKEN_KEY, refreshToken)
  }
}

// 清除两个 storage 中的 token 对（logout / 登录失败回滚用）
function clearTokenPair(): void {
  sessionStorage.removeItem(ACCESS_TOKEN_KEY)
  sessionStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

interface AuthState {
  isAuthenticated: boolean
  user: AuthUser | null
  token: string | null
  refreshToken: string | null
  keepLogin: boolean
  // 登录入口：写入 token + storage（根据 keepLogin 选位置），设 isAuthenticated=true
  // user 允许 null：登录流程通常先 login(token, null, refresh, keepLogin) 写 token
  // 让拦截器可用，再 getMe() 拿 user，最后 setUser(user) 补全
  login: (token: string, user: AuthUser | null, refreshToken: string, keepLogin: boolean) => void
  logout: () => void
  // getMe 成功后补全 user 信息（login 时 user 未知场景）
  setUser: (user: AuthUser) => void
  // 刷新 token 后调用：更新 store + 同步写入 storage（保持原存储位置，_keepLogin 不变）
  setTokens: (accessToken: string, refreshToken: string) => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      isAuthenticated: false,
      user: null,
      token: null,
      refreshToken: null,
      keepLogin: true,
      login: (token, user, refreshToken, keepLogin) => {
        _keepLogin = keepLogin
        writeTokenPair(token, refreshToken)
        set({ isAuthenticated: true, token, user, refreshToken, keepLogin })
      },
      logout: () => {
        clearTokenPair()
        _keepLogin = true // 重置为默认值，避免影响下次登录的 storage 选择
        set({
          isAuthenticated: false,
          token: null,
          user: null,
          refreshToken: null,
          keepLogin: true,
        })
      },
      setUser: (user) => {
        set({ user })
      },
      setTokens: (accessToken, refreshToken) => {
        writeTokenPair(accessToken, refreshToken)
        set({ token: accessToken, refreshToken })
      },
    }),
    {
      name: 'auth-store',
      storage: createJSONStorage(() => dynamicStorage),
      // 持久化登录态、用户、token、refreshToken、keepLogin（不持久化方法）
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        user: state.user,
        token: state.token,
        refreshToken: state.refreshToken,
        keepLogin: state.keepLogin,
      }),
      // 恢复时同步 _keepLogin 标志，确保后续 setItem 写入正确位置
      onRehydrateStorage: () => (state) => {
        if (state) {
          _keepLogin = state.keepLogin
        }
      },
    },
  ),
)
