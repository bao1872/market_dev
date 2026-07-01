// axios 实例 + 请求/响应拦截器
// baseURL=/api 由 Vite 代理转发到后端 http://localhost:8000
//
// [API 客户端] - apiClient：带 Bearer Token 注入 + 401 单例 refresh 重试，供所有需要认证的端点使用
// [API 客户端] - publicApiClient：无 Authorization 注入、无 401 refresh 逻辑，供 login/register/refresh/public beta 等公开端点使用
import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { useAuthStore, ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY } from '../store/auth'
import { useToast } from '../store/toast'

export const apiClient = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// [API 客户端] - 公开接口客户端：baseURL 与 apiClient 相同，但不挂载任何拦截器，避免公开端点携带旧 token 或触发 refresh
export const publicApiClient = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 读取 access token：优先 sessionStorage（未保持登录的当前会话），再 localStorage（保持登录或 capture 模式）
function getAccessToken(): string | null {
  return sessionStorage.getItem(ACCESS_TOKEN_KEY) ?? localStorage.getItem(ACCESS_TOKEN_KEY)
}

// 读取 refresh token：优先级同 access token
function getRefreshToken(): string | null {
  return sessionStorage.getItem(REFRESH_TOKEN_KEY) ?? localStorage.getItem(REFRESH_TOKEN_KEY)
}

// 写入新 token 对：委托给 store.setTokens，由 store 根据 _keepLogin 决定存储位置
function setTokens(accessToken: string, refreshToken: string): void {
  useAuthStore.getState().setTokens(accessToken, refreshToken)
}

// ===== 单例 refresh token 刷新 =====
// 多个并发请求同时 401 时，只发起一次 refresh，其余请求 await 同一个 Promise 实现排队重试
// （Promise 复用模式：所有并发 401 调用者拿到同一个 refreshPromise，无需显式队列）
let isRefreshing = false
let refreshPromise: Promise<string> | null = null

async function refreshTokenSingleton(): Promise<string> {
  // 已有刷新在进行：复用同一个 Promise（单例），调用者 await 后自动等待结果
  if (isRefreshing && refreshPromise) {
    return refreshPromise
  }
  isRefreshing = true
  refreshPromise = (async () => {
    try {
      const refreshToken = getRefreshToken()
      if (!refreshToken) throw new Error('No refresh token available')
      // 使用 publicApiClient 调用，绕过 apiClient 拦截器，避免 refresh 请求 401 又触发刷新
      const response = await publicApiClient.post('/auth/refresh', {
        refresh_token: refreshToken,
      })
      const { access_token, refresh_token: new_refresh_token } = response.data
      setTokens(access_token, new_refresh_token)
      return access_token
    } catch (error) {
      // 刷新失败：logout 清登录态（清 token + store 状态）
      // 调用者 catch 后负责跳转登录页
      useAuthStore.getState().logout()
      throw error
    } finally {
      isRefreshing = false
      refreshPromise = null
    }
  })()
  return refreshPromise
}

// 请求拦截器：注入 Bearer Token
apiClient.interceptors.request.use(
  (config) => {
    // 优先从 URL 读取 capture token（截图模式），其次 storage（session 优先，local 兜底）
    const urlToken = new URLSearchParams(window.location.search).get('token')
    const token = urlToken || getAccessToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截器：401 处理（单例刷新 + 重试一次）+ 403 显式提示
// [capture-mode] 截图模式下（URL 含 capture=feishu）不刷新不跳转：
// capture token 无 refresh token，调用 admin API 会 401，若跳转登录页会导致
// StockDetailPage 卸载、data-render-ready 永远 false、截图超时 502
// [Auth] - 描述: 403 与 401 处理完全隔离——403 仅显示 toast 提示权限不足，不清除登录态、不跳转
apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as
      | (InternalAxiosRequestConfig & { _retry?: boolean })
      | undefined
    // [Auth] - 描述: 403 权限不足：显示 toast 友好提示，不清除 token、不跳转（与 401 隔离）
    if (error.response?.status === 403) {
      useToast.getState().show('权限不足', '当前账号无权访问该资源')
      return Promise.reject(error)
    }
    // 非 401 或无 config：直接 reject
    if (error.response?.status !== 401 || !originalRequest) {
      return Promise.reject(error)
    }

    const isCaptureMode =
      new URLSearchParams(window.location.search).get('capture') === 'feishu'
    // capture 模式：不刷新、不跳转，直接 reject（capture token 无 refresh）
    if (isCaptureMode) {
      return Promise.reject(error)
    }

    // 已重试过的请求再次 401：不再刷新，直接 reject（避免无限循环）
    if (originalRequest._retry) {
      return Promise.reject(error)
    }
    originalRequest._retry = true

    try {
      const newToken = await refreshTokenSingleton()
      // 用新 token 重试原请求
      originalRequest.headers.Authorization = `Bearer ${newToken}`
      return apiClient(originalRequest)
    } catch (refreshError) {
      // 刷新失败：refreshTokenSingleton 内部已 logout，这里负责跳转登录页
      // （避免在多处重复 logout / 跳转）
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
      return Promise.reject(refreshError)
    }
  },
)
