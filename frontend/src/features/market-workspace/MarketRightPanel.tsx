// [CHANGE-20260730-012] /market 右栏简化：只保留 MiniKlineCard + 第一金字塔 compact
// 删除 AtomicFactsPanel、更多观察、moreOpen、相关CSS和请求，标题改"第一金字塔"
import { FirstPyramidPanel } from '@/features/stock-research/FirstPyramidPanel'
import { MiniKlineCard } from './MiniKlineCard'
import { resolveMarketRightPanelState } from './marketRightPanelState'
import styles from './MarketRightPanel.module.scss'

interface MarketRightPanelProps {
  symbol: string | null
}

export function MarketRightPanel({ symbol }: MarketRightPanelProps) {
  // 挂载状态由纯函数推导（change010Contract：禁止内联三元表达式复制挂载逻辑）
  const { showPyramid } = resolveMarketRightPanelState(symbol)
  return (
    <div className={styles.panel}>
      <div className={styles.klineFixed}>
        <MiniKlineCard symbol={symbol} />
      </div>
      <div className={styles.stateScroll}>
        {showPyramid && symbol && (
          <FirstPyramidPanel symbol={symbol} variant="compact" />
        )}
      </div>
    </div>
  )
}
