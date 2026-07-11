// [RouteStructure] - 描述: 可测试的纯路由层级结构定义（无 React 依赖）
// 由 App.tsx 的 routeConfig 引用，供路由契约测试断言路由层级关系。
// 本文件只描述路由 path 和守卫/shell 标记，不包含页面组件。

// 守卫/壳层标记（用于测试断言，不参与运行时渲染）
export type GuardType = 'public' | 'protected' | 'subscriber' | 'admin' | 'capture' | 'redirect'
export type ShellType = 'none' | 'user' | 'admin'

export interface RouteNode {
  path?: string
  guard: GuardType
  shell: ShellType
  // 用于重定向的目标路径（guard='redirect' 时有效）
  redirectTo?: string
  children?: RouteNode[]
}

// 完整路由层级结构（与 App.tsx routeConfig 一一对应，修改路由时必须同步）
export const ROUTE_STRUCTURE: RouteNode[] = [
  { path: '/', guard: 'public', shell: 'none' },
  { path: '/login', guard: 'public', shell: 'none' },
  { path: '/subscription-expired', guard: 'public', shell: 'none' },
  { path: '/membership-expired', guard: 'redirect', shell: 'none', redirectTo: '/subscription-expired' },
  // Capture 路由：位于所有守卫和壳层之外
  { path: '/capture/stock/:symbol', guard: 'capture', shell: 'none' },
  // ProtectedLayout 组
  {
    guard: 'protected',
    shell: 'none',
    children: [
      // UserAppShell 组
      {
        guard: 'protected',
        shell: 'user',
        children: [
          // SubscriberRoute 组
          {
            guard: 'subscriber',
            shell: 'user',
            children: [
              { path: '/market', guard: 'subscriber', shell: 'user' },
              { path: '/replay', guard: 'subscriber', shell: 'user' },
              { path: '/stock/:symbol', guard: 'subscriber', shell: 'user' },
            ],
          },
          // 仅认证（不强制订阅）
          { path: '/settings', guard: 'protected', shell: 'user' },
          { path: '/messages', guard: 'protected', shell: 'user' },
        ],
      },
      // AdminRoute + AdminAppShell 组
      {
        guard: 'admin',
        shell: 'none',
        children: [
          {
            guard: 'admin',
            shell: 'admin',
            children: [
              { path: '/admin', guard: 'admin', shell: 'admin' },
              { path: '/admin/overview', guard: 'admin', shell: 'admin' },
              { path: '/admin/users', guard: 'admin', shell: 'admin' },
              { path: '/admin/beta-applications', guard: 'admin', shell: 'admin' },
              { path: '/admin/strategies', guard: 'admin', shell: 'admin' },
              { path: '/admin/jobs', guard: 'admin', shell: 'admin' },
              { path: '/admin/after-close', guard: 'admin', shell: 'admin' },
              { path: '/admin/stock-debug', guard: 'admin', shell: 'admin' },
              { path: '/admin/stock-debug/:symbol', guard: 'admin', shell: 'admin' },
            ],
          },
        ],
      },
      // 兼容重定向
      { path: '/overview', guard: 'redirect', shell: 'none', redirectTo: '/market' },
      { path: '/watchlist', guard: 'redirect', shell: 'none', redirectTo: '/market?scope=watchlist' },
      { path: '/screener', guard: 'redirect', shell: 'none', redirectTo: '/market' },
    ],
  },
  // 兜底
  { path: '*', guard: 'redirect', shell: 'none', redirectTo: '/market' },
]

// 递归查找匹配 path 的路由节点及其所有祖先节点
export function findRouteNode(
  routes: RouteNode[],
  path: string,
): { node: RouteNode; ancestors: RouteNode[] } | null {
  function search(
    list: RouteNode[],
    ancestors: RouteNode[],
  ): { node: RouteNode; ancestors: RouteNode[] } | null {
    for (const node of list) {
      if (node.path === path) {
        return { node, ancestors }
      }
      if (node.children) {
        const result = search(node.children, [...ancestors, node])
        if (result) return result
      }
    }
    return null
  }
  return search(routes, [])
}

// 判断路径是否经过指定守卫（检查祖先链中是否含该 guard）
export function hasGuardInChain(routes: RouteNode[], path: string, guard: GuardType): boolean {
  const result = findRouteNode(routes, path)
  if (!result) return false
  if (result.node.guard === guard) return true
  return result.ancestors.some((a) => a.guard === guard)
}

// 判断路径是否经过指定壳层（检查祖先链和自身）
export function hasShellInChain(routes: RouteNode[], path: string, shell: ShellType): boolean {
  const result = findRouteNode(routes, path)
  if (!result) return false
  if (result.node.shell === shell) return true
  return result.ancestors.some((a) => a.shell === shell)
}
