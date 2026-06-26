// axios 实例 + 请求/响应拦截器
// baseURL=/api 由 Vite 代理转发到后端 http://localhost:8000
import axios from 'axios'
import { useAuthStore } from '../store/auth'

export const apiClient = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器：注入 Bearer Token
apiClient.interceptors.request.use(
  (config) => {
    // 优先从 URL 读取 capture token（截图模式），其次 localStorage
    const urlToken = new URLSearchParams(window.location.search).get('token')
    const token = urlToken || localStorage.getItem('auth_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截器：401 清除登录态（localStorage + zustand store）并跳转登录页
// [capture-mode] 截图模式下（URL 含 capture=feishu）不跳转登录页：
// capture token 无 admin 角色，调用 admin API 会 401，若跳转登录页会导致
// StockDetailPage 卸载、data-render-ready 永远 false、截图超时 502
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const isCaptureMode = new URLSearchParams(window.location.search).get('capture') === 'feishu'
      if (!isCaptureMode) {
        // 同步清除 localStorage.auth_token 和 zustand auth-store 状态
        // 防止 token 过期后路由守卫误放行（isAuthenticated 仍为 true）
        useAuthStore.getState().logout()
        if (window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
      }
    }
    return Promise.reject(error)
  },
)
