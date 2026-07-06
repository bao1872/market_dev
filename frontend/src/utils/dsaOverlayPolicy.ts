// [DSA Overlay Policy] - 描述: DSA overlay 周期策略与禁用提示文案
//   纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   供 StrategyChart 复用

/**
 * DSA overlay 在非 1d 周期下的禁用提示文案
 *
 * 产品设计：DSA 是日线级别结构锚，15m/1h 不渲染 DSA VWAP overlay。
 * 用户切换到 15m/1h 时，DSA 图层按钮 disabled，并显示此提示。
 *
 * 注意：右侧结构状态面板仍可显示 daily DSA 背景和 m15 response，
 * 不受图层禁用影响。
 */
export const DSA_DISABLED_HINT =
  'DSA VWAP 当前仅支持日线结构锚；15m/1h 请使用 Swing、BB、SQZMOM。'

/**
 * 判断当前 timeframe 是否需要校验 DSA source mismatch
 *
 * - 1d: 需要（DSA 在 1d 渲染，必须校验 source 对齐）
 * - 15m/1h/1w/1mo: 不需要（DSA 不在这些周期渲染，mismatch 校验无意义）
 *
 * 修复根因（PR #31）：
 *   之前 15m/1h 下仍校验 mismatch，当 Redis 旧缓存返回旧格式
 *   source_bar_times 时会误报 "DSA 数据源不一致"，但 DSA 本就不在 15m 渲染。
 *   修复后：15m/1h 不校验 mismatch，避免误报。
 */
export function shouldCheckDsaMismatch(timeframe: string): boolean {
  return timeframe === '1d'
}
