// [V2.1] capability-based access helpers (PRD §9, §10)
// 前端权限/导航/额度 UI 唯一真源为 /me/access 返回的 V2.1 capabilities + watchlist_limits。
// 禁止从旧 features/monitor_limit/subscription 重新推导模块权限。
//
// 后端为最终 403 边界：前端守卫仅用于隐藏无权限入口和友好提示，
// 手工 URL 访问无权限页面时，后端 API 仍返回 403。
import type { AuthUser } from '../store/auth.ts'
import type { CapabilityKey } from '../api/endpoints.ts'
import type { AppNavItem } from '../navigation/appNavigation.ts'
import { APP_ROUTES } from '../navigation/appNavigation.ts'

/** 检查用户是否拥有指定能力的 active grant（或为 admin） */
export function hasCapability(user: AuthUser | null, key: CapabilityKey): boolean {
  if (!user) return false
  if (user.is_admin) return true
  return user.capabilities?.[key]?.active === true
}

/** /market 入口：watchlist_management 或 market_screening 任一即可 */
export function canAccessMarket(user: AuthUser | null): boolean {
  if (!user) return false
  if (user.is_admin) return true
  return (
    hasCapability(user, 'watchlist_management') ||
    hasCapability(user, 'market_screening')
  )
}

/** /stock/:symbol 详情：需要 market_screening（DSA/K线/指标） */
export function canAccessStockDetail(user: AuthUser | null): boolean {
  return hasCapability(user, 'market_screening')
}

/** /replay 复盘：需要 market_screening（复盘依赖选股数据） */
export function canAccessReplay(user: AuthUser | null): boolean {
  return hasCapability(user, 'market_screening')
}

/** 自选 scope/按钮：需要 watchlist_management */
export function canAccessWatchlist(user: AuthUser | null): boolean {
  return hasCapability(user, 'watchlist_management')
}

/** 复盘管理：需要 review_management（功能未上线，仅保留权限判定） */
export function canAccessReview(user: AuthUser | null): boolean {
  return hasCapability(user, 'review_management')
}

/** 根据能力过滤普通用户一级导航项 */
export function getVisibleUserNavItems(
  user: AuthUser | null,
  allItems: AppNavItem[],
): AppNavItem[] {
  return allItems.filter((item) => {
    // 行情：watchlist_management 或 market_screening 任一即可
    if (item.path === APP_ROUTES.market) return canAccessMarket(user)
    // 复盘：需要 market_screening
    if (item.path === APP_ROUTES.replay) return canAccessReplay(user)
    // 其他默认可见（如设置/消息由调用方单独判断）
    return true
  })
}

/** 自选额度描述文案（用于 UI 展示） */
export function formatWatchlistQuota(user: AuthUser | null): string {
  if (!user) return ''
  const limits = user.watchlist_limits
  if (!limits) return ''
  if (limits.is_admin_unlimited) {
    return `自选 ${limits.watchlist_current_count}（不限）`
  }
  const limit = limits.watchlist_stock_limit
  if (limit === null || limit === undefined) {
    return `自选 ${limits.watchlist_current_count}（无额度）`
  }
  return `自选 ${limits.watchlist_current_count} / ${limit}`
}

/** 自选是否超限（用于 UI 高亮提示） */
export function isWatchlistOverLimit(user: AuthUser | null): boolean {
  if (!user) return false
  return user.watchlist_limits?.watchlist_over_limit === true
}
