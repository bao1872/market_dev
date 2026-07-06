// [DSA Overlay Policy] - 描述: DSA overlay 周期策略与提示文案
//   纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   供 StrategyChart 复用

/**
 * DSA overlay 是否允许在指定周期渲染
 *
 * [PR #32] - DSA VWAP 支持全周期（1d/15m/1h/1w/1mo），不再 1d-only。
 * 1d 是主结构锚，非 1d 是验证图层（用于核查该周期结构）。
 */
export function shouldAllowDsaOverlay(timeframe: string): boolean {
  return ['1d', '15m', '1h', '1w', '1mo'].includes(timeframe)
}

/**
 * 判断当前 timeframe 是否需要校验 DSA source mismatch
 *
 * [PR #32] - DSA 全周期渲染，全部需要校验 source mismatch。
 * 之前 PR #31 仅 1d 校验（DSA 不在 15m/1h 渲染），现已改为全周期支持。
 */
export function shouldCheckDsaMismatch(timeframe: string): boolean {
  return shouldAllowDsaOverlay(timeframe)
}

/**
 * DSA overlay 在指定周期的 title 提示文案
 *
 * - 1d: "DSA VWAP 日线结构锚。"（主趋势锚）
 * - 非 1d: "DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。"
 */
export function DSA_TITLE_HINT(timeframe: string): string {
  if (timeframe === '1d') {
    return 'DSA VWAP 日线结构锚。'
  }
  return 'DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。'
}
