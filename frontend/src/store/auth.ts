// 认证状态 store（zustand + persist 自定义持久化）
// 管理登录态、当前用户、access_token + refresh_token
// 持久化策略（"保持登录"开关）：
//   - keepLogin=true  → localStorage（关闭浏览器后保留登录态）
//   - keepLogin=false → sessionStorage（关闭标签页后清除，更安全）
// token 同步写入 auth_token / auth_refresh_token key，供 client.ts 拦截器直接读取
// （避免 store 初始化时序问题，且兼容 capture 模式写 localStorage 的场景）
import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import { apiClient } from '../api/client'

// [Auth] - 描述: AuthUser 当前用户身份 + AccessProfile 权限上下文（对齐后端 LoginResponse 字段）
// 替代旧 role: 'admin' | 'member' 单值，改用 is_admin + roles[] + subscription_active 等
// 唯一真源为后端 get_access_context，前端不在本地计算权限
// [Phase 5B-2 PRD60 PA-01] 新增 capabilities 字段（三类独立权限状态，由 /v1/me/access 刷新）
export interface CapabilityInfo {
  active: boolean
  expires_at: string | null
  watchlist_limit: number | null
}

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
  // [Phase 5B-2 PRD60 PA-01] 三类独立权限状态（self_selection/market_data/research_replay）
  // 登录时默认空对象，由 revalidateAccess 调用 /v1/me/access 后填充
  capabilities: Record<string, CapabilityInfo>
  // [权限模型 V2] 统一权限画像字段（由 /v1/me/access 填充）
  default_route?: string | null
  active_capability_keys?: string[]
  capability_source?: string
}

// token 在 storage 中的 key（client.ts 拦截器读取这两个 key）
export const ACCESS_TOKEN_KEY = 'auth_token'
export const REFRESH_TOKEN_KEY = 'auth_refresh_token'
// [capture-mode] capture token 独立 storage key，与普通 auth_token 隔离
// 普通 apiClient 只读 ACCESS_TOKEN_KEY，不读 CAPTURE_TOKEN_KEY（避免 capture token 污染业务 API）
export const CAPTURE_TOKEN_KEY = 'capture_token'

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

// [Auth] - 描述: AccessContextResponse 后端 /v1/me/access 响应类型（对齐 AccessProfileResponse 12 字段）
// 用于 revalidateAccess 刷新前端权限上下文，避免重复定义（与 endpoints.ts AccessProfile 同源）
// [Phase 5B-2 PRD60 PA-01] 新增 capabilities 字段
interface AccessContextResponse {
  user_id: string
  account_status: string
  roles: string[]
  is_admin: boolean
  is_member: boolean
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
  capabilities: Record<string, CapabilityInfo>
  // [权限模型 V2] 统一权限画像字段
  default_route?: string | null
  active_capability_keys?: string[]
  capability_source?: string
  diagnostics?: string[]
}

// [权限模型 V2] 权限加载状态机
export type HydrationStatus = 'hydrating' | 'hydrated'
export type AccessStatus = 'idle' | 'loading' | 'ready' | 'error'

interface AuthState {
  isAuthenticated: boolean
  user: AuthUser | null
  token: string | null
  refreshToken: string | null
  keepLogin: boolean
  // [权限模型 V2] persist 恢复状态 + 权限补水状态机
  hydrationStatus: HydrationStatus
  accessStatus: AccessStatus
  accessError: string | null
  // [兼容] accessLoading 保留给旧守卫引用（等价 accessStatus === 'loading'）
  accessLoading: boolean
  // 登录入口：写入 token + storage（根据 keepLogin 选位置），设 isAuthenticated=true
  // user 允许 null：登录流程通常先 login(token, null, refresh, keepLogin) 写 token
  // 让拦截器可用，再 getMe() 拿 user，最后 setUser(user) 补全
  login: (token: string, user: AuthUser | null, refreshToken: string, keepLogin: boolean) => void
  logout: () => void
  // getMe 成功后补全 user 信息（login 时 user 未知场景）
  setUser: (user: AuthUser) => void
  // 刷新 token 后调用：更新 store + 同步写入 storage（保持原存储位置，_keepLogin 不变）
  setTokens: (accessToken: string, refreshToken: string) => void
  // [Auth] - 描述: 刷新页面后校验权限上下文，防止 persist 的 user.capabilities 为空/过期。
  // 调用 GET /v1/me/access 获取最新权限，更新完整画像（capabilities/default_route）。
  // 成功后若当前在 /forbidden 且 default_route 非 /forbidden，replace 到 default_route。
  revalidateAccess: () => Promise<void>
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      isAuthenticated: false,
      user: null,
      token: null,
      refreshToken: null,
      keepLogin: true,
      hydrationStatus: 'hydrating',
      accessStatus: 'idle',
      accessError: null,
      accessLoading: false,
      login: (token, user, refreshToken, keepLogin) => {
        _keepLogin = keepLogin
        writeTokenPair(token, refreshToken)
        set({
          isAuthenticated: true,
          token,
          user,
          refreshToken,
          keepLogin,
          accessStatus: 'idle',
          accessError: null,
        })
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
          hydrationStatus: 'hydrated',
          accessStatus: 'idle',
          accessError: null,
        })
      },
      setUser: (user) => {
        // [权限模型 V2] 只合并 user 基础字段，不覆盖已加载的 capabilities
        const current = useAuthStore.getState().user
        set({
          user: {
            id: current?.id ?? user.id,
            name: current?.name ?? user.name ?? user.email,
            email: current?.email ?? user.email,
            is_admin: current?.is_admin ?? false,
            roles: current?.roles ?? [],
            subscription_active: current?.subscription_active ?? false,
            plan_code: current?.plan_code ?? null,
            plan_display_name: current?.plan_display_name ?? null,
            expires_at: current?.expires_at ?? null,
            features: current?.features ?? [],
            limits: current?.limits ?? {},
            capabilities: current?.capabilities ?? {},
            default_route: current?.default_route,
            active_capability_keys: current?.active_capability_keys,
            capability_source: current?.capability_source,
          } as AuthUser,
        })
      },
      setTokens: (accessToken, refreshToken) => {
        writeTokenPair(accessToken, refreshToken)
        set({ token: accessToken, refreshToken })
      },
      revalidateAccess: async () => {
        const { isAuthenticated, token } = useAuthStore.getState()
        // 未登录或 token 缺失不校验（无 token 无法调 /v1/me/access）
        if (!isAuthenticated || !token) {
          set({ accessStatus: 'idle', accessError: null, accessLoading: false })
          return
        }
        // [capture-mode] 截图模式跳过：capture token 是临时 admin token，无 refresh，不参与权限校验
        if (new URLSearchParams(window.location.search).get('capture') === 'feishu') return
        set({ accessStatus: 'loading', accessLoading: true, accessError: null })
        try {
          const { data } = await apiClient.get<AccessContextResponse>('/v1/me/access')
          const currentUser = useAuthStore.getState().user
          // [权限模型 V2] 更新完整权限画像（capabilities/default_route/active_keys/source）
          const updated: AuthUser = {
            id: data.user_id,
            name: currentUser?.name ?? currentUser?.email ?? data.user_id,
            email: currentUser?.email ?? data.user_id,
            is_admin: data.is_admin,
            roles: data.roles,
            subscription_active: data.subscription_active,
            plan_code: data.plan_code,
            plan_display_name: data.plan_display_name,
            expires_at: data.expires_at,
            features: data.features,
            limits: data.limits,
            capabilities: data.capabilities ?? {},
            default_route: data.default_route,
            active_capability_keys: data.active_capability_keys ?? [],
            capability_source: data.capability_source,
          }
          set({ user: updated, accessStatus: 'ready', accessLoading: false, accessError: null })
          // [权限模型 V2] 权限补水成功后，若当前在 /forbidden 且 default_route 非 /forbidden，
          // 跳回 default_route（不再由 subscription_active 决定入口）
          const defaultRoute = data.default_route
          if (defaultRoute && defaultRoute !== '/forbidden') {
            const currentPath = window.location.pathname + window.location.search
            if (currentPath === '/forbidden' || window.location.pathname === '/forbidden') {
              window.location.replace(defaultRoute)
            }
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : '权限加载失败'
          set({ accessStatus: 'error', accessError: msg, accessLoading: false })
          // /v1/me/access 失败（401 等）：由 client.ts 拦截器统一处理 refresh 或跳登录；
          // 不得伪装成 403（accessStatus=error 由页面展示"权限加载失败"）
        }
      },
    }),
    {
      name: 'auth-store',
      storage: createJSONStorage(() => dynamicStorage),
      // [权限模型 V2] persist 版本迁移：旧版本（无 accessStatus）持久化的 user.capabilities
      // 可能是空/过期，不作为权威状态。迁移后 accessStatus=idle，触发自动重拉 /v1/me/access。
      version: 2,
      migrate: (persistedState) => {
        // _version：persist 版本号（本迁移只处理旧版本→v2，旧 capabilities 不作权威）
        const state = (persistedState ?? {}) as Partial<AuthState>
        const migrated = {
          ...state,
          hydrationStatus: 'hydrated' as const,
          // 旧版本无 accessStatus → 设为 idle，由守卫触发 revalidateAccess 重拉
          accessStatus: 'idle' as const,
          accessError: null,
          accessLoading: false,
        }
        // 旧版本 user.capabilities 可能为空：不删除 user（保 token/登录态），
        // 但 accessStatus=idle 保证受保护路由先显示 loading 而非立即判无权限。
        return migrated as unknown as AuthState
      },
      // 持久化登录态、用户、token、refreshToken、keepLogin（不持久化方法/accessLoading/accessError）
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
          // [权限模型 V2] 恢复后 hydration 完成；守卫据此在 accessStatus=idle 时触发 revalidateAccess
          state.hydrationStatus = 'hydrated'
        }
      },
    },
  ),
)
