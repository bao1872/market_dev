// [Navigation] - 描述: 单一导航/路由常量真源（避免路径散落在各页面）
// PRD V1.0 阶段一（路由与壳层）确立：
//   普通用户主入口 = /market（行情，渲染 MarketWorkspacePage）
//   复盘占位路由 /replay（功能规划中，不伪造业务）
//   消息 /messages、设置 /settings 进入右上角账户菜单
//   管理后台独立壳层 AdminAppShell，承载 /admin/*
//   Capture 路由 /capture/stock/:symbol 位于两套壳层之外
//   旧路由 /overview → /market、/watchlist → /market?scope=watchlist、/screener → /market 仅作兼容重定向
//   旧 WatchlistPage.tsx 和 IndexPage.tsx 已删除（统一行情工作区改造）
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行，便于路由契约测试。

export const APP_ROUTES = {
  market: '/market',
  screener: '/screener',
  replay: '/replay',
  messages: '/messages',
  settings: '/settings',
  admin: '/admin',
  adminOverview: '/admin/overview',
  adminUsers: '/admin/users',
  adminBeta: '/admin/beta-applications',
  // C8: adminStrategies 已废弃，重定向到 adminAfterClose（DSA 运行能力保留在盘后流水线）
  adminJobs: '/admin/jobs',
  adminAfterClose: '/admin/after-close',
  adminStockDebug: '/admin/stocks',
  adminStockDebugDetail: '/admin/stocks/:symbol/debug',
  capture: '/capture/stock/:symbol',
  login: '/login',
  subscriptionExpired: '/subscription-expired',
} as const

// 个股详情路由（动态 symbol）
export function stockRoute(symbol: string): string {
  return `/stock/${symbol}`
}

// 管理员个股调试详情路由（动态 symbol）
export function adminStockDebugRoute(symbol: string): string {
  return `/admin/stocks/${symbol}/debug`
}

// 默认登录/兜底入口（替换旧 /overview）
export const DEFAULT_ENTRY = APP_ROUTES.market

export interface AppNavItem {
  path: string
  label: string
}

// 普通用户一级导航（行情 + 复盘；消息/设置不在此处）
export const USER_NAV_ITEMS: AppNavItem[] = [
  { path: APP_ROUTES.market, label: '行情' },
  { path: APP_ROUTES.replay, label: '复盘' },
]

// 管理员控制台导航（仅 AdminAppShell 侧栏使用）
// P1: 移除"策略目录"（多策略组合已废弃，只保留 dsa_selector + watchlist_monitor）
export const ADMIN_NAV_ITEMS: AppNavItem[] = [
  { path: APP_ROUTES.admin, label: '系统概览' },
  { path: APP_ROUTES.adminUsers, label: '用户与套餐' },
  { path: APP_ROUTES.adminBeta, label: '内测申请' },
  { path: APP_ROUTES.adminJobs, label: '任务与事件' },
  { path: APP_ROUTES.adminAfterClose, label: '盘后流水线' },
  { path: APP_ROUTES.adminStockDebug, label: '个股调试' },
]

export interface AccountMenuItem {
  path: string
  label: string
  // 仅管理员可见（如管理后台入口）
  adminOnly: boolean
}

// 账户菜单项（消息、设置对所有用户；管理后台仅管理员）
export const ACCOUNT_MENU_ITEMS: AccountMenuItem[] = [
  { path: APP_ROUTES.messages, label: '消息中心', adminOnly: false },
  { path: APP_ROUTES.settings, label: '通知与设置', adminOnly: false },
  { path: APP_ROUTES.admin, label: '管理后台', adminOnly: true },
]

// 过滤当前用户可见的账户菜单项（管理员额外显示管理后台入口）
export function getAccountMenuItems(isAdmin: boolean): AccountMenuItem[] {
  return ACCOUNT_MENU_ITEMS.filter((item) => !item.adminOnly || isAdmin)
}

// 账户菜单 variant：决定是否追加"返回行情"（admin 壳层）
export type AccountMenuVariant = 'user' | 'admin'

// 根据 isAdmin + variant 构建账户菜单项（AccountMenu 唯一真源）
// - 基础项：消息 + 设置（对所有用户可见）
// - variant='user' + isAdmin=true：消息 + 设置 + 管理后台（admin 从用户壳层进入后台）
// - variant='user' + isAdmin=false：仅消息 + 设置
// - variant='admin'：消息 + 设置 + 返回行情（不重复"管理后台"）
export function getAccountMenuItemsForVariant(
  isAdmin: boolean,
  variant: AccountMenuVariant,
): AccountMenuItem[] {
  const baseItems = getAccountMenuItems(isAdmin)
  if (variant === 'admin') {
    // AdminAppShell 上下文：移除"管理后台"项，追加"返回行情"
    return [
      ...baseItems.filter((item) => item.path !== APP_ROUTES.admin),
      { path: APP_ROUTES.market, label: '返回行情', adminOnly: false },
    ]
  }
  // UserAppShell 上下文：getAccountMenuItems 已按 isAdmin 过滤
  // admin 用户看到消息+设置+管理后台；普通用户只看到消息+设置
  return baseItems
}

// 旧路由兼容重定向映射
export const LEGACY_REDIRECTS: Record<string, string> = {
  '/overview': APP_ROUTES.market,
  '/watchlist': `${APP_ROUTES.market}?scope=watchlist`,
  '/screener': APP_ROUTES.market,
  // [Phase4] 旧管理员调试路由 → 新路由（前后端统一使用 symbol）
  '/admin/stock-debug': APP_ROUTES.adminStockDebug,
}

// 生成 react-router 重定向路由项（供 App.tsx 使用，保持单一真源）
export function legacyRedirectEntries(): { path: string; to: string }[] {
  return Object.entries(LEGACY_REDIRECTS).map(([path, to]) => ({ path, to }))
}
