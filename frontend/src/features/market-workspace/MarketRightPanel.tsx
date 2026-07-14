// CHANGE-20260713-010: /market 右栏容器
// 组合 MiniKlineCard（顶部）+ EventStatePanel（底部），保持 EventStatePanel 单一职责不变。
// 面板收起时由父组件不挂载本组件，bars/context 请求均为 0。
// symbol 为 null 时 MiniKlineCard 内部显示提示，EventStatePanel 不渲染。
import { EventStatePanel } from '@/features/research-context/EventStatePanel'
import { MiniKlineCard } from './MiniKlineCard'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  return (
    <>
      <MiniKlineCard symbol={symbol} />
      {symbol && <EventStatePanel symbol={symbol} />}
    </>
  )
}
