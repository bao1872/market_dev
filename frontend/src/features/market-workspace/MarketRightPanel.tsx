// CHANGE-20260713-010 + Atomic Fact Contract V1: /market 右栏容器
// [Round 2026-07-28-4] 固定结构：小K线固定区（230px不收缩）+ 状态滚动区（compact第一金字塔 + 更多观察）
// [P0 修复] 更多观察使用 useState 控制展开，收起时不挂载 AtomicFactsPanel，请求为 0。
// [CHANGE-20260728-007] 挂载状态由 resolveMarketRightPanelState 纯函数统一推导。
// 小K线区域 flex:0 0 230px，下方内容滚动，不压缩K线。
import { useState } from 'react'
import { AtomicFactsPanel } from '@/features/research-context/AtomicFactsPanel'
import { FirstPyramidPanel } from '@/features/stock-research/FirstPyramidPanel'
import { MiniKlineCard } from './MiniKlineCard'
import { resolveMarketRightPanelState } from './marketRightPanelState'
import styles from './MarketRightPanel.module.scss'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  const [moreOpen, setMoreOpen] = useState(false)
  const state = resolveMarketRightPanelState(symbol, moreOpen)

  return (
    <div className={styles.panel}>
      <div className={styles.klineFixed}>
        <MiniKlineCard symbol={symbol} />
      </div>
      <div className={styles.stateScroll}>
        {state.showPyramid && symbol && (
          <FirstPyramidPanel symbol={symbol} variant="compact" />
        )}
        {state.showMoreObservation && symbol && (
          <div className={styles.moreObservation}>
            <button
              type="button"
              className={styles.moreObservationToggle}
              onClick={() => setMoreOpen((v) => !v)}
              aria-expanded={moreOpen}
            >
              {moreOpen ? '▼ 更多观察' : '▶ 更多观察'}
            </button>
            {state.showAtomicFacts && <AtomicFactsPanel symbol={symbol} variant="compact" />}
          </div>
        )}
      </div>
    </div>
  )
}
