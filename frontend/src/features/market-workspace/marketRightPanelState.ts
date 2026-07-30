// [MarketRightPanelState] - 描述: /market 右栏挂载状态纯函数（无 React 依赖）
// [CHANGE-20260730-012] 简化：只保留 MiniKlineCard + FirstPyramidPanel compact
// 删除 moreObservation / AtomicFactsPanel / moreOpen 相关状态
//
// 纯 TS 模块（无 React 依赖），可被 node --experimental-strip-types 直接运行。

export type MarketRightPanelSection = 'mini-kline' | 'first-pyramid'

export interface MarketRightPanelState {
  /** 是否挂载第一金字塔（symbol 存在即挂载） */
  showPyramid: boolean
  /** 固定的 UI 顺序：小K线 → 第一金字塔 */
  sectionOrder: readonly MarketRightPanelSection[]
}

/**
 * 解析 /market 右栏挂载状态。
 *
 * 规则：
 *   - symbol=null：只显示小K线空态，不挂载第一金字塔
 *   - symbol 存在：显示第一金字塔 compact
 *
 * @param symbol 当前股票代码（null 表示无选中）
 */
export function resolveMarketRightPanelState(
  symbol: string | null,
): MarketRightPanelState {
  const hasSymbol = Boolean(symbol)
  return {
    showPyramid: hasSymbol,
    sectionOrder: ['mini-kline', 'first-pyramid'] as const,
  }
}
