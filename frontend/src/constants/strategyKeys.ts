export const STRATEGY_KEYS = {
  DSA_SELECTOR: 'dsa_selector',
  WATCHLIST_MONITOR: 'watchlist_monitor',
} as const

export type StrategyKey = (typeof STRATEGY_KEYS)[keyof typeof STRATEGY_KEYS]
