// [AccessGuard] - 描述: 受保护路由的权限状态机唯一真源（纯 TS，无 React 依赖）
//
// 背景（权限模型 V2 + 本轮 A 修复）：
//   1. access 补水（/v1/me/access）此前由 ProtectedLayout 内的一次性 ref 触发，
//      「一次机会被消费后即使前置条件改变也不再重试」，属于结构缺陷。
//   2. 所有 `accessStatus !== 'ready'` 曾被渲染成纯黑 div，导致 idle/loading/error
//      三种完全不同的状态在视觉上不可区分，且失败没有重试入口。
//
// 本模块把「要不要发权限请求」「该显示什么」收敛为纯函数，供 App.tsx 守卫直接调用，
// 并可由 node --test 直接断言（同 navigation/capabilities.ts 的既有做法）。
//
// 约束：
//   - 不引入第二套 local loading state；状态完全由 auth store 驱动。
//   - 不扩大任何用户权限：只有 ready 且确认具备所需 capability 才放行。

/** persist 恢复状态（与 store/auth.ts HydrationStatus 对齐） */
export type HydrationStatus = 'hydrating' | 'hydrated'

/** 权限补水状态机（与 store/auth.ts AccessStatus 对齐） */
export type AccessStatus = 'idle' | 'loading' | 'ready' | 'error'

/**
 * 受保护路由（ProtectedLayout）的决策结果。
 *
 * - hydrating:          persist 尚未恢复 → 显示 loading（不得黑屏）
 * - unauthenticated:    未登录 → 重定向 /login
 * - needs_revalidation: 已登录且权限补水尚未开始 → 触发 revalidateAccess()
 * - pending:            权限补水进行中 → 显示 loading
 * - error:              权限补水失败 → 交由 capability 层显示错误 + retry
 * - ready:              放行
 */
export type ProtectedGateDecision =
  | 'hydrating'
  | 'unauthenticated'
  | 'needs_revalidation'
  | 'pending'
  | 'error'
  | 'ready'

/**
 * 计算受保护路由决策（状态驱动，无一次性标记）。
 *
 * 关键性质：只要前置条件后到（例如 hydration 先未完成、随后 hydrated + authenticated），
 * 决策会自然从 hydrating 变为 needs_revalidation，从而仍能触发补水——不再依赖
 * 「一生只尝试一次」的 ref。
 */
export function resolveProtectedGate(input: {
  hydrationStatus: HydrationStatus
  isAuthenticated: boolean
  accessStatus: AccessStatus
}): ProtectedGateDecision {
  const { hydrationStatus, isAuthenticated, accessStatus } = input
  if (hydrationStatus !== 'hydrated') return 'hydrating'
  if (!isAuthenticated) return 'unauthenticated'
  if (accessStatus === 'idle') return 'needs_revalidation'
  if (accessStatus === 'loading') return 'pending'
  if (accessStatus === 'error') return 'error'
  return 'ready'
}

/** capability 守卫决策结果 */
export type CapabilityGateDecision = 'pending' | 'error' | 'allow' | 'forbidden'

/** capability active 状态的最小结构（复用 capabilities.ts 唯一真源，禁止重复定义） */
import type { CapabilityStateLike } from './capabilities'

export type { CapabilityStateLike }

/**
 * 计算 capability 守卫决策。
 *
 * 状态语义（不得混淆）：
 * - error:    权限补水失败 → 必须显示可见错误 + retry，绝不黑屏
 * - pending:  idle/loading/hydrating → 必须显示可见 loading，绝不黑屏
 * - allow:    ready 且（admin 或满足所需 capability）
 * - forbidden: ready 且后端确认不具备所需 capability → 跳 /forbidden
 *
 * mode='any' 对应 CapabilityAnyRoute；mode='all' 对应 CapabilityRoute。
 */
export function resolveCapabilityGate(input: {
  accessStatus: AccessStatus
  isAdmin: boolean
  capabilities: Record<string, CapabilityStateLike | undefined> | null | undefined
  required: readonly string[]
  mode?: 'any' | 'all'
}): CapabilityGateDecision {
  const { accessStatus, isAdmin, capabilities, required, mode = 'any' } = input
  if (accessStatus === 'error') return 'error'
  if (accessStatus !== 'ready') return 'pending'
  if (isAdmin) return 'allow'
  const satisfies = (capability: string): boolean =>
    capabilities?.[capability]?.active === true
  const ok =
    mode === 'all' ? required.every(satisfies) : required.some(satisfies)
  return ok ? 'allow' : 'forbidden'
}
