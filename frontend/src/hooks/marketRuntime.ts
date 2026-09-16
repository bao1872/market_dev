// Market runtime utility（市场时钟）
//
// [S3-B] 由 useApi.ts 抽出。原因：isInTradingHours() 同时被 useApi.ts 保留的 hooks
// 和新的 useWatchlistApi.ts 使用；若后者直接 import useApi 会形成循环依赖
// （useApi → useWatchlistApi → useApi），故下沉为单向依赖的窄模块。
//
// 内容仅市场时钟：模块级缓存 + 上海时区 fallback，不含任何 React Query hook。
// 依赖方向：api/market ← marketRuntime ← useApi / useWatchlistApi。
//
// 设计说明：isInTradingHours() 是同步函数（用于 refetchInterval 回调），
// 无法直接 await 后端 API。通过模块级缓存 + AppShell 30s 轮询更新，
// 使交易时段判断与后端保持一致；缓存未填充时使用 Intl 上海时区 fallback。

import type { MarketStatus } from '../api/market'

let _cachedMarketStatus: MarketStatus | null = null

/** 更新市场状态缓存（由 AppShell 的轮询逻辑调用） */
export function setCachedMarketStatus(status: MarketStatus | null): void {
  _cachedMarketStatus = status
}

/** 获取当前缓存的市场状态（可用于 UI 显示） */
export function getCachedMarketStatus(): MarketStatus | null {
  return _cachedMarketStatus
}

/** 上海时区 fallback：使用 Intl.DateTimeFormat 固定 Asia/Shanghai 判断交易时段 */
function isInTradingHoursShanghaiFallback(): boolean {
  // 使用 en-US 获取稳定的 weekday 缩写，避免 zh-CN 在不同平台的差异
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const parts = fmt.formatToParts(new Date())
  const weekday = parts.find((p) => p.type === 'weekday')?.value ?? ''
  const hourStr = parts.find((p) => p.type === 'hour')?.value ?? '0'
  const minuteStr = parts.find((p) => p.type === 'minute')?.value ?? '0'
  // hour 可能是 "24"（午夜），归一化为 0
  const hour = parseInt(hourStr, 10) % 24
  const minute = parseInt(minuteStr, 10)
  const dayMap: Record<string, number> = {
    Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6,
  }
  const day = dayMap[weekday] ?? -1
  const isWeekday = day >= 1 && day <= 5
  const timeVal = hour * 60 + minute
  const isMorningSession = timeVal >= 570 && timeVal <= 690 // 9:30-11:30
  const isAfternoonSession = timeVal >= 780 && timeVal <= 900 // 13:00-15:00
  return isWeekday && (isMorningSession || isAfternoonSession)
}

/**
 * 判断当前是否在 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00，上海时间）
 *
 * 优先级：
 * 1. 后端 /market/status 缓存（由 UserAppShell/AdminAppShell 30s 轮询更新，包含交易日判断）
 * 2. Intl.DateTimeFormat 固定 Asia/Shanghai 时区的本地 fallback（仅 weekday+时间，不含节假日）
 */
export function isInTradingHours(): boolean {
  if (_cachedMarketStatus) {
    return _cachedMarketStatus.is_trading_hours
  }
  return isInTradingHoursShanghaiFallback()
}
