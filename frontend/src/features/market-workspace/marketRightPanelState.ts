// [MarketRightPanelState] - 描述: /market 右栏挂载状态纯函数（无 React 依赖）
// CHANGE-20260728-007: 从源码字符串测试迁移为行为测试。
// 新契约：MiniKlineCard → FirstPyramidPanel → 更多观察 → AtomicFactsPanel（仅 moreOpen=true）。
//
// 纯 TS 模块（无 React 依赖），可被 node --experimental-strip-types 直接运行。
// MarketRightPanel.tsx 必须消费本函数，禁止内联三元表达式复制挂载逻辑。

export type MarketRightPanelSection = 'mini-kline' | 'first-pyramid' | 'more-observation'

export interface MarketRightPanelState {
  /** 是否挂载第一金字塔（symbol 存在即挂载） */
  showPyramid: boolean
  /** 是否渲染"更多观察"入口（symbol 存在即渲染） */
  showMoreObservation: boolean
  /** 是否挂载 AtomicFactsPanel（symbol 存在且 moreOpen=true） */
  showAtomicFacts: boolean
  /** 固定的 UI 顺序：小K线 → 第一金字塔 → 更多观察 */
  sectionOrder: readonly MarketRightPanelSection[]
}

/**
 * 解析 /market 右栏挂载状态。
 *
 * 规则：
 *   - symbol=null：只显示小K线空态，不挂载第一金字塔和 AtomicFactsPanel
 *   - symbol 存在、moreOpen=false：显示第一金字塔，不挂载 AtomicFactsPanel
 *   - symbol 存在、moreOpen=true：挂载 AtomicFactsPanel（一次）
 *
 * @param symbol 当前股票代码（null 表示无选中）
 * @param moreOpen "更多观察"是否展开
 */
export function resolveMarketRightPanelState(
  symbol: string | null,
  moreOpen: boolean,
): MarketRightPanelState {
  const hasSymbol = Boolean(symbol)
  return {
    showPyramid: hasSymbol,
    showMoreObservation: hasSymbol,
    showAtomicFacts: hasSymbol && moreOpen,
    sectionOrder: ['mini-kline', 'first-pyramid', 'more-observation'] as const,
  }
}
