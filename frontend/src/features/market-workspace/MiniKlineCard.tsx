// CHANGE-20260713-010: 右栏小 K 线卡片
// CHANGE-20260714-001: 扩展为五周期（15m/60m/日/周/月），删除标题，移除图内 TV 标志
// CHANGE-011 P0: 重写 viewport 计算，提取 computeMiniKlineViewport 纯函数
//   - 动态 visible range：floor((contentWidth - 56) / 5)，clamp 到 per-timeframe 区间
//   - 右侧保留 3 bar 空位（最新 K 线不紧贴价格轴）
//   - setData 后只执行一次 setVisibleLogicalRange，不再先 fitContent（避免竞态）
//   - 价格轴 autoScale + scaleMargins {top:0.12, bottom:0.12}，覆盖可见 K 线影线
//   - ResizeObserver 使用整数 contentRect.width，宽度变化时 requestAnimationFrame 重应用 range
//   - 切周期不复用上一周期 logical range
// 使用 lightweight-charts v4 渲染简化 K 线（仅 K 线 + 价格轴 + 简化时间轴）。
// 不显示指标/成交量/Node/事件标记/工具栏。
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
import {
  computeMiniKlineViewport,
  MIN_PRICE_SCALE_WIDTH,
} from './miniKlineViewport'

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

// 图表正文高度（保持在 190-210px 区间）
const CHART_HEIGHT = 200

export function MiniKlineCard({ symbol }: MiniKlineCardProps) {
  const [timeframe, setTimeframe] = useState<MiniKlineTimeframe>('1d')
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // 当前容器宽度（整数 px），用于依赖变化时重新应用 range
  const containerWidthRef = useRef<number>(0)

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
        // CHANGE-011 P0: 价格轴最小宽度 56px（与 viewport 计算的 priceScaleWidth 对齐）
        minimumWidth: MIN_PRICE_SCALE_WIDTH,
        // CHANGE-011 P0: 启用 autoScale + scaleMargins，覆盖可见 K 线影线
        autoScale: true,
        scaleMargins: {
          top: 0.12,
          bottom: 0.12,
        },
      },
      timeScale: {
        borderColor: '#263440',
        timeVisible: false,
        secondsVisible: false,
        // CHANGE-011 P0: 禁用 shiftVisibleRangeOnNewBar，避免新数据推入时 range 漂移
        shiftVisibleRangeOnNewBar: false,
      },
      crosshair: {
        mode: 0, // Normal
      },
      width: containerRef.current.clientWidth,
      height: CHART_HEIGHT,
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

    // 响应式调整宽度 + 重新应用 viewport range
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        // CHANGE-011 P0: 使用整数 contentRect.width 避免亚像素抖动
        const intWidth = Math.floor(entry.contentRect.width)
        if (intWidth <= 0) continue
        const chart = chartRef.current
        if (!chart) continue
        chart.applyOptions({ width: intWidth })
        containerWidthRef.current = intWidth
        // 宽度变化时在 rAF 中重新应用 range（避免在 ResizeObserver 回调中直接操作引发布局抖动）
        requestAnimationFrame(() => {
          applyViewportRange(intWidth)
        })
      }
    })
    resizeObserver.observe(containerRef.current)

    // 初始化 containerWidthRef
    containerWidthRef.current = Math.floor(containerRef.current.clientWidth)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      containerWidthRef.current = 0
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // CHANGE-011 P0: 应用 viewport range（setData 后只调用一次 setVisibleLogicalRange）
  // 不再先 fitContent 再设 range（避免竞态导致 range 被覆盖）
  function applyViewportRange(width: number) {
    const chart = chartRef.current
    if (!chart) return
    const vp = computeMiniKlineViewport(bars.length, timeframe, width)
    if (vp.visibleBars <= 0) return
    chart.timeScale().setVisibleLogicalRange({
      from: vp.from,
      to: vp.to,
    })
  }

  // 更新数据 + 按周期切换时间轴显示（intraday 显示时间，日周月只显示日期）
  // 切周期不复用上一周期 logical range（每次重新计算）
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

    // CHANGE-011 P0: setData 后只执行一次 setVisibleLogicalRange
    // 不再调用 fitContent（避免先全屏再设 range 的竞态）
    const width = containerWidthRef.current || Math.floor(containerRef.current?.clientWidth ?? 0)
    if (width > 0) {
      // 在 rAF 中应用，确保 setData 已完成布局
      requestAnimationFrame(() => {
        applyViewportRange(width)
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
