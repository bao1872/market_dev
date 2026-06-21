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
    const token = localStorage.getItem('auth_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截器：401 清除登录态（localStorage + zustand store）并跳转登录页
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // 同步清除 localStorage.auth_token 和 zustand auth-store 状态
      // 防止 token 过期后路由守卫误放行（isAuthenticated 仍为 true）
      useAuthStore.getState().logout()
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)
