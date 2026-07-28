// CHANGE-20260713-010 + Atomic Fact Contract V1: /market 右栏容器
// [Round 2026-07-28-2] 固定结构：MiniKlineCard（顶部）+ compact 第一金字塔 + 更多观察（AtomicFactsPanel 默认收起）
// [P0 修复] 更多观察使用 useState 控制展开，收起时不挂载 AtomicFactsPanel，请求为 0。
// symbol 为 null 时 MiniKlineCard 内部显示提示，第一金字塔和 AtomicFactsPanel 不渲染。
import { useState } from 'react'
import { AtomicFactsPanel } from '@/features/research-context/AtomicFactsPanel'
import { FirstPyramidPanel } from '@/features/stock-research/FirstPyramidPanel'
import { MiniKlineCard } from './MiniKlineCard'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  const [moreOpen, setMoreOpen] = useState(false)

  return (
    <>
      <MiniKlineCard symbol={symbol} />
      {symbol && (
        <FirstPyramidPanel symbol={symbol} variant="compact" />
      )}
      {symbol && (
        <div className="market-more-observation">
          <button
            type="button"
            className="more-observation-toggle"
            onClick={() => setMoreOpen((v) => !v)}
            aria-expanded={moreOpen}
          >
            {moreOpen ? '▼ 更多观察' : '▶ 更多观察'}
          </button>
          {moreOpen && <AtomicFactsPanel symbol={symbol} variant="compact" />}
        </div>
      )}
    </>
  )
}
