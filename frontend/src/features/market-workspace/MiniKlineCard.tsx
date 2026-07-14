// CHANGE-20260713-010: 右栏小 K 线卡片
// 使用 lightweight-charts v4 渲染简化 K 线（仅 K 线 + 价格轴 + 简化时间轴）。
// 不显示指标/成交量/Node/事件标记/工具栏。
// 支持日线/周线/月线三按钮，默认日线；股票切换时保留用户选择的周期。
import { useEffect, useRef, useState } from 'react'
import {
  createChart,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type Time,
} from 'lightweight-charts'
import { useMiniKlineData, type MiniKlineTimeframe } from './useMiniKlineData'

interface MiniKlineCardProps {
  symbol: string | null
}

const TIMEFRAME_OPTIONS: Array<{ value: MiniKlineTimeframe; label: string }> = [
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '1mo', label: '月线' },
]

export function MiniKlineCard({ symbol }: MiniKlineCardProps) {
  const [timeframe, setTimeframe] = useState<MiniKlineTimeframe>('1d')
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)

  const { bars, isLoading, isError } = useMiniKlineData(symbol, timeframe)

  // 创建 chart 实例（仅一次）
  useEffect(() => {
    if (!containerRef.current) return
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0A0F14' },
        textColor: '#98A1B3',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#161F29' },
        horzLines: { color: '#161F29' },
      },
      rightPriceScale: {
        borderColor: '#263440',
      },
      timeScale: {
        borderColor: '#263440',
        timeVisible: false,
        secondsVisible: false,
      },
      crosshair: {
        mode: 0, // Normal
      },
      width: containerRef.current.clientWidth,
      height: 200,
    })
    chartRef.current = chart

    const series = chart.addCandlestickSeries({
      upColor: '#FF4D4F', // A股红涨
      downColor: '#22C55E', // 绿跌
      borderUpColor: '#FF4D4F',
      borderDownColor: '#22C55E',
      wickUpColor: '#FF4D4F',
      wickDownColor: '#22C55E',
    })
    seriesRef.current = series

    // 响应式调整宽度
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        chart.applyOptions({ width: entry.contentRect.width })
      }
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  // 更新数据
  useEffect(() => {
    const series = seriesRef.current
    const chart = chartRef.current
    if (!series || !chart) return

    const data: CandlestickData[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }))

    series.setData(data)
    chart.timeScale().fitContent()
  }, [bars])

  return (
    <div className="mini-kline-card">
      <div className="mini-kline-header">
        <span className="mini-kline-title">小 K 线</span>
        <div className="mini-kline-tabs">
          {TIMEFRAME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className={`mini-kline-tab ${timeframe === opt.value ? 'active' : ''}`}
              onClick={() => setTimeframe(opt.value)}
              aria-pressed={timeframe === opt.value}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
      {!symbol && <div className="mini-kline-empty">选择股票查看小 K 线</div>}
      {symbol && isLoading && <div className="mini-kline-loading">加载中…</div>}
      {symbol && isError && !isLoading && <div className="mini-kline-error">加载失败</div>}
      {symbol && !isLoading && !isError && bars.length === 0 && (
        <div className="mini-kline-empty">暂无数据</div>
      )}
      <div
        ref={containerRef}
        className="mini-kline-chart"
        style={{
          display: !symbol || isLoading || isError || bars.length === 0 ? 'none' : 'block',
        }}
      />
    </div>
  )
}
