// [MarketDashboard] - lightweight-charts 多线封装（复用，不另写第二套 chart implementation）
// 关键：
// - breadth(0~1) 放 left scale，EW index(~100) 放 right scale，不粗暴共用同一数轴；
// - 80%/20% 仅作参考线（createPriceLine），不加任何解释性判断；
// - null 断线：buildLineData 已省略无效点（whitespace gap），绝不变 0。
import { useEffect, useRef } from 'react'
import { createChart, LineStyle, type IChartApi, type ISeriesApi } from 'lightweight-charts'
import { buildLineData } from './dashboardLogic'
import type { BreadthPoint } from './types'
import styles from './dashboard.module.scss'

export interface BreadthLineSpec {
  field: keyof BreadthPoint
  label: string
  color: string
  scale: 'left' | 'right'
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
  referenceLines?: ReferenceLine[]
  height?: number
}

export default function BreadthChart({ points, series, referenceLines, height = 320 }: BreadthChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const hasLeft = series.some((s) => s.scale === 'left')
    const hasRight = series.some((s) => s.scale === 'right')

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
      leftPriceScale: { visible: hasLeft, borderColor: '#e3e6eb' },
      rightPriceScale: { visible: hasRight, borderColor: '#e3e6eb' },
      timeScale: { borderColor: '#e3e6eb', timeVisible: false },
      crosshair: { mode: 0 },
    })

    const created: ISeriesApi<'Line'>[] = series.map((spec) => {
      const s = chart.addLineSeries({
        color: spec.color,
        lineWidth: 2,
        priceScaleId: spec.scale === 'left' ? 'left' : '',
      })
      s.setData(buildLineData(points, 'trade_date', spec.field))
      return s
    })

    if (referenceLines && referenceLines.length > 0 && created.length > 0) {
      // 参考线挂在 left scale 的线上（80%/20% 属于 breadth 量纲）；无 left 线时挂首个线。
      const anchor = created.find((_, i) => series[i].scale === 'left') ?? created[0]
      referenceLines.forEach((rl) => {
        anchor.createPriceLine({
          price: rl.price,
          color: rl.color ?? '#9aa3af',
          lineWidth: 1,
          lineStyle: rl.dashed === false ? LineStyle.Solid : LineStyle.Dashed,
          axisLabelVisible: true,
          title: rl.label ?? '',
        })
      })
    }

    return () => {
      chart.remove()
    }
  }, [points, series, referenceLines, height])

  return <div ref={containerRef} className={styles.chart} />
}
