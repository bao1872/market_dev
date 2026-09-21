// [MarketDashboard][R3A] - 重点板块比较图（thin wrapper；交互唯一实现在 MultiLineChart）
//
// 每条线来自 API 返回的 rebased EW index（前端不再二次归一化）；null 仍断线；
// legend 显示 board name 且可点击 hide/show；industry / concept 混合不被前端拒绝。
import { useMemo } from 'react'
import MultiLineChart from './MultiLineChart'
import { buildLineData } from './dashboardLogic'
import { EW_LINE_WIDTH } from './chartTheme'
import type { CompareBoard } from './types'

export interface CompareChartProps {
  boards: CompareBoard[]
  height?: number
}

export default function CompareChart({ boards, height = 360 }: CompareChartProps) {
  const multiSeries = useMemo(
    () =>
      boards.map((board) => ({
        key: board.board_id,
        label: board.board_name,
        // color 省略 → MultiLineChart 用 shared palette 按顺序分配（绝不使用涨跌色）
        scale: 'right' as const,
        lineWidth: EW_LINE_WIDTH,
        data: buildLineData(board.points, 'trade_date', 'ew_index'),
      })),
    [boards],
  )

  return <MultiLineChart series={multiSeries} height={height} />
}
