// [MarketDashboard] - 重点板块比较图（每条线来自 API 返回的 rebased EW index，前端不再二次归一化）
// null 仍断线；legend 显示 board name；industry / concept 混合不被前端拒绝。
import { useEffect, useRef } from 'react'
import { createChart, type IChartApi } from 'lightweight-charts'
import { buildLineData } from './dashboardLogic'
import type { CompareBoard } from './types'
import styles from './dashboard.module.scss'

const PALETTE = [
  '#2962ff', '#00b28a', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4', '#ec4899', '#84cc16',
  '#f97316', '#6366f1', '#14b8a6', '#e11d48', '#a855f7', '#22c55e', '#0ea5e9', '#d946ef',
  '#65a30d', '#fb7185', '#0d9488', '#7c3aed',
]

export interface CompareChartProps {
  boards: CompareBoard[]
  height?: number
}

export default function CompareChart({ boards, height = 360 }: CompareChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart: IChartApi = createChart(el, {
      autoSize: true,
      height,
      layout: {
        background: { color: '#ffffff' },
        textColor: '#374151',
        fontFamily: 'SFMono-Regular, monospace',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: '#eef1f4' },
        horzLines: { color: '#eef1f4' },
      },
      rightPriceScale: { borderColor: '#e3e6eb' },
      timeScale: { borderColor: '#e3e6eb', timeVisible: false },
      crosshair: { mode: 0 },
    })
    boards.forEach((board, i) => {
      const s = chart.addLineSeries({ color: PALETTE[i % PALETTE.length], lineWidth: 2, priceScaleId: '' })
      s.setData(buildLineData(board.points, 'trade_date', 'ew_index'))
    })
    return () => {
      chart.remove()
    }
  }, [boards, height])

  return (
    <div>
      <div ref={containerRef} className={styles.chart} />
      {boards.length > 0 && (
        <div className={styles.legend}>
          {boards.map((b, i) => (
            <span key={b.board_id} className={styles.legendItem}>
              <span className={styles.legendDot} style={{ background: PALETTE[i % PALETTE.length] }} />
              {b.board_name}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
