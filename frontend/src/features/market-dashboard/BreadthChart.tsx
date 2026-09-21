// [MarketDashboard][R3A] - 多线图表（thin wrapper；图表/交互唯一实现在 MultiLineChart）
//
// 关键（由 MultiLineChart 保证）：breadth(0~1) 放 left scale、EW index(~100) 放 right scale；
// null 断线（whitespace gap，绝不变 0）；legend 可点击 hide/show 且不重建 chart。
import { useMemo } from 'react'
import MultiLineChart from './MultiLineChart'
import { buildLineData } from './dashboardLogic'
import { REVIEW_TOKENS, type ChartReferenceLine } from './chartTheme'
import type { FixedScaleRange, LineWidth } from './lineSeriesController'
import type { BreadthPoint } from './types'

export interface BreadthLineSpec {
  field: keyof BreadthPoint
  label: string
  /** 省略则由 shared palette 分配（普通 MA / EW 绝不使用 market.up / market.down）。 */
  color?: string
  scale: 'left' | 'right'
  lineWidth?: LineWidth
  /** breadth 0..1 语义：左轴按百分比呈现（数据保持 0..1）。 */
  breadthPercent?: boolean
  /** 固定价格轴区间（breadth 用 [0,1]）。 */
  fixedScaleRange?: FixedScaleRange
}

export interface ReferenceLine {
  price: number
  color?: string
  label?: string
  dashed?: boolean
}

export interface BreadthChartProps {
  points: BreadthPoint[]
  series: BreadthLineSpec[]
  referenceLines?: readonly ReferenceLine[]
  height?: number
}

export default function BreadthChart({ points, series, referenceLines, height = 320 }: BreadthChartProps) {
  const multiSeries = useMemo(
    () =>
      series.map((spec) => ({
        key: String(spec.field),
        label: spec.label,
        color: spec.color,
        scale: spec.scale,
        lineWidth: spec.lineWidth,
        breadthPercent: spec.breadthPercent,
        fixedScaleRange: spec.fixedScaleRange,
        data: buildLineData(points, 'trade_date', spec.field),
      })),
    [points, series],
  )

  const refs = useMemo<ChartReferenceLine[] | undefined>(
    () =>
      referenceLines?.map((line) => ({
        price: line.price,
        color: line.color ?? REVIEW_TOKENS.text.muted,
        label: line.label ?? '',
        dashed: line.dashed,
      })),
    [referenceLines],
  )

  return <MultiLineChart series={multiSeries} referenceLines={refs} height={height} />
}
