// Auth / Access React Query hooks owner
//
// [S3-A] 由 useApi.ts 迁出。useApi.ts 以兼容 barrel 重新导出这些 hooks
// （`import { useMe } from '@/hooks/useApi'` 调用方零改动）。
//
// 依赖方向：auth.ts ← useAuthApi ← useApi(barrel)。
// 本文件不得 import ../hooks/useApi（否则形成循环依赖）。
//
// 合同（与迁移前逐字等价）：query key / staleTime / invalidation 语义完全不变。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as authApi from '../api/auth'
import type { LoginRequest, RegisterRequest } from '../api/auth'

// [S3-A] 窄常量：auth 相关查询的 staleTime（0 = 始终视为过期，每次挂载都重新请求）。
// 迁移前复用 useApi.ts 的 STALE_MESSAGES(=0)；此处独立定义，避免为共享一个常量
// 新建全局 cache-policy 抽象（不属于 S3-A）。
const STALE_AUTH = 0

// ============================================================
// ===== Auth hooks =====
// ============================================================

/** 获取当前用户信息（始终刷新） */
export function useMe() {
  return useQuery({
    queryKey: ['me'],
    queryFn: authApi.getMe,
    staleTime: STALE_AUTH,
  })
}

/** 获取当前用户会员状态（始终刷新） */
export function useMyMembership() {
  return useQuery({
    queryKey: ['me', 'membership'],
    queryFn: authApi.getMyMembership,
    staleTime: STALE_AUTH,
  })
}

/** 登录变更 */
export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ email, password }: LoginRequest) => authApi.login(email, password),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
    },
  })
}

/** 注册变更 */
export function useRegister() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: RegisterRequest) => authApi.register(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
    },
  })
}

/** 续期变更 */
export function useRenew() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (inviteCode: string) => authApi.renew(inviteCode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me', 'membership'] })
    },
  })
}

/** Token 刷新变更 */
export function useRefreshToken() {
  return useMutation({
    mutationFn: (refreshToken: string) => authApi.refreshToken(refreshToken),
  })
}
