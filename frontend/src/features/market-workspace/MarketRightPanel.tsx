// CHANGE-20260713-010 + Atomic Fact Contract V1: /market 右栏容器
// [Round 2026-07-28-2] 固定结构：MiniKlineCard（顶部）+ compact 第一金字塔 + 更多观察（AtomicFactsPanel 默认收起）
// 面板收起时由父组件不挂载本组件，bars/context 请求均为 0。
// symbol 为 null 时 MiniKlineCard 内部显示提示，第一金字塔和 AtomicFactsPanel 不渲染。
import { AtomicFactsPanel } from '@/features/research-context/AtomicFactsPanel'
import { FirstPyramidPanel } from '@/features/stock-research/FirstPyramidPanel'
import { MiniKlineCard } from './MiniKlineCard'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  return (
    <>
      <MiniKlineCard symbol={symbol} />
      {symbol && (
        <FirstPyramidPanel symbol={symbol} variant="compact" />
      )}
      {symbol && (
        <details className="market-more-observation">
          <summary>更多观察</summary>
          <AtomicFactsPanel symbol={symbol} variant="compact" />
        </details>
      )}
    </>
  )
}
