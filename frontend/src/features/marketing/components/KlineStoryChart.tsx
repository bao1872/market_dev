import {
  ColorType,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type WhitespaceData,
} from 'lightweight-charts'
import { useEffect, useLayoutEffect, useRef } from 'react'

import type { StoryCandle, StoryEvent } from '../demo/storyTypes'

type Props = {
  candles: StoryCandle[]
  /** frame = 已展示的 K 线根数 */
  frame: number
  events?: StoryEvent[]
  height?: number
}

// 读取现有 :root CSS token（variables.scss），不在 TS 里新建色板
function readToken(name: string, fallback: string) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim()
  return value || fallback
}

export function KlineStoryChart({ candles, frame, events = [], height = 360 }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const fittedRef = useRef(false)

  useLayoutEffect(() => {
    const host = hostRef.current
    if (!host) return

    const up = readToken('--up', '#FF4D4F')
    const down = readToken('--down', '#22C55E')
    const panel = readToken('--panel', '#111A23')
    const border = readToken('--border', '#263440')
    const muted = readToken('--muted', '#8493A0')

    const chart = createChart(host, {
      width: host.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: panel },
        textColor: muted,
      },
      grid: {
        vertLines: { color: border },
        horzLines: { color: border },
      },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, timeVisible: false },
    })

    const series = chart.addCandlestickSeries({
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
    })

    chartRef.current = chart
    seriesRef.current = series

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: host.clientWidth })
    })
    resizeObserver.observe(host)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      fittedRef.current = false
    }
  }, [height])

  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series) return

    // 未来 K 线以 WhitespaceData 占位：不显示，但 X 轴空间固定
    const visibleData: (CandlestickData<Time> | WhitespaceData<Time>)[] = candles.map(
      (bar, index) => {
        if (index < frame) {
          return {
            time: bar.time,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
          }
        }
        return { time: bar.time }
      },
    )

    series.setData(visibleData)

    const brand = readToken('--brand', '#00F6C2')
    const muted = readToken('--muted', '#8493A0')

    const markers: SeriesMarker<Time>[] = events
      .filter((event) => event.frame <= frame)
      .map((event) => ({
        time: candles[event.frame - 1].time,
        position: 'aboveBar',
        shape: 'arrowUp',
        color: event.tone === 'positive' ? brand : muted,
        text: event.label,
      }))

    series.setMarkers(markers)

    // 只在首次建立坐标范围，之后不再 fitContent，避免播放时 K 线不断缩放跳动
    if (!fittedRef.current) {
      chart.timeScale().fitContent()
      fittedRef.current = true
    }
  }, [candles, events, frame])

  return <div ref={hostRef} />
}
