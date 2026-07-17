// CHANGE-20260713-010 + Atomic Fact Contract V1: /market 右栏容器
// 组合 MiniKlineCard（顶部）+ AtomicFactsPanel（底部，compact 形态）。
// 面板收起时由父组件不挂载本组件，bars/context 请求均为 0。
// symbol 为 null 时 MiniKlineCard 内部显示提示，AtomicFactsPanel 不渲染。
import { AtomicFactsPanel } from '@/features/research-context/AtomicFactsPanel'
import { MiniKlineCard } from './MiniKlineCard'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  return (
    <>
      <MiniKlineCard symbol={symbol} />
      {symbol && <AtomicFactsPanel symbol={symbol} variant="compact" />}
    </>
  )
}
