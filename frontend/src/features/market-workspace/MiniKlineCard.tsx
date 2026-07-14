// CHANGE-20260713-010: 右栏小 K 线卡片
// CHANGE-20260714-001: 扩展为五周期（15m/60m/日/周/月），删除标题，移除图内 TV 标志
// 使用 lightweight-charts v4 渲染简化 K 线（仅 K 线 + 价格轴 + 简化时间轴）。
// 不显示指标/成交量/Node/事件标记/工具栏。
// 支持五周期按钮，默认日线；股票切换时保留用户选择的周期。
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

// 五周期按钮：15m/60m(1h API)/日/周/月
const TIMEFRAME_OPTIONS: Array<{ value: MiniKlineTimeframe; label: string }> = [
  { value: '15m', label: '15m' },
  { value: '1h', label: '60m' },
  { value: '1d', label: '日' },
  { value: '1w', label: '周' },
  { value: '1mo', label: '月' },
]

const INTRADAY_TIMEFRAMES: ReadonlySet<MiniKlineTimeframe> = new Set(['15m', '1h'])

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
        // CHANGE-20260714-001: 移除图内 TradingView 标志（署名改到设置页）
        attributionLogo: false,
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

  // 更新数据 + 按周期切换时间轴显示（intraday 显示时间，日周月只显示日期）
  useEffect(() => {
    const series = seriesRef.current
    const chart = chartRef.current
    if (!series || !chart) return

    // intraday 周期显示时间轴（HH:MM），日周月只显示日期
    chart.applyOptions({
      timeScale: { timeVisible: INTRADAY_TIMEFRAMES.has(timeframe) },
    })

    const data: CandlestickData[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }))

    series.setData(data)
    chart.timeScale().fitContent()
  }, [bars, timeframe])

  return (
    <div className="mini-kline-card">
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
      {!symbol && <div className="mini-kline-empty">选择股票查看</div>}
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
