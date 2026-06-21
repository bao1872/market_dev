// 冒烟测试：验证核心模块能正确导入与类型解析
// 运行方式：npx tsc --noEmit（已包含在 npm run build 的 tsc -b 阶段）
// 目的：确保 api/client、api/types、store/auth、App(router) 模块图完整可解析
import { apiClient } from './api/client'
import type { ApiResponse, PageResult, PageState } from './api/types'
import { useAuthStore } from './store/auth'
import { router } from './App'

export function smoke(): {
  api: typeof apiClient
  auth: typeof useAuthStore
  router: typeof router
} {
  return {
    api: apiClient,
    auth: useAuthStore,
    router,
  }
}

// 类型解析验证：确保泛型与联合类型可用
export type _SmokePageState = PageState
export type _SmokeResponse = ApiResponse<PageResult<unknown>>
