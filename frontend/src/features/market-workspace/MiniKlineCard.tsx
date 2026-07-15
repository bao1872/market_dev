// CHANGE-20260713-010: 右栏小 K 线卡片
// CHANGE-20260714-001: 扩展为五周期（15m/60m/日/周/月），删除标题，移除图内 TV 标志
// CHANGE-20260715-002 P0: 重写 viewport 计算 + autoscaleInfoProvider
//   - 目标根数按周期固定：15m=48、60m=44、日=40、周=36、月=30
//   - barSpacing clamp 5.5–8px
//   - 左侧 1-2 根留白，右侧 3 根留白
//   - setData 后 requestAnimationFrame 中调用 setVisibleLogicalRange（不调用 fitContent）
//   - autoscaleInfoProvider 扩展价格范围：上方 12%，下方 15%
//   - rightPriceScale autoScale + scaleMargins {top:0.08, bottom:0.08}，minimumWidth=56
//   - 图表高度固定 190px，无多余 min-height 或底部空白
//   - 切周期不复用上一周期 logical range
// CHANGE-20260715-006: 根治 ResizeObserver/rAF 闭包问题
//   - 新增 barsLengthRef、timeframeRef 持有最新值，避免 ResizeObserver 捕获首次 render 闭包
//   - applyViewportRange 改为 useCallback 稳定函数，从 refs 读取最新 bars.length/timeframe
//   - 新增 rafIdRef 跟踪 pending rAF，symbol/timeframe/bars/width 变化时取消上一个 rAF
//   - ResizeObserver 回调调用稳定 applyViewportRange，不再持有首次 render 闭包
// 使用 lightweight-charts v4 渲染简化 K 线（仅 K 线 + 价格轴 + 简化时间轴）。
// 不显示指标/成交量/Node/事件标记/工具栏。
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createChart,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type Time,
  type AutoscaleInfo,
} from 'lightweight-charts'
import { useMiniKlineData, type MiniKlineTimeframe } from './useMiniKlineData'
import {
  computeMiniKlineViewport,
  computeAutoscaleRange,
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

// 图表正文高度（CHANGE-20260715-002: 固定 190px）
const CHART_HEIGHT = 190

export function MiniKlineCard({ symbol }: MiniKlineCardProps) {
  const [timeframe, setTimeframe] = useState<MiniKlineTimeframe>('1d')
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // 当前容器宽度（整数 px），用于依赖变化时重新应用 range
  const containerWidthRef = useRef<number>(0)

  // CHANGE-20260715-006: barsLengthRef/timeframeRef 持有最新值
  // ResizeObserver 在 mount 时创建一次，其回调若直接闭包捕获 bars/timeframe
  // 将永远使用首次 render 的值（空数组 + '1d'），导致宽度变化时 range 计算错误
  const barsLengthRef = useRef<number>(0)
  const timeframeRef = useRef<MiniKlineTimeframe>(timeframe)
  // CHANGE-20260715-006: rafIdRef 跟踪 pending rAF，deps 变化时取消避免应用 stale range
  const rafIdRef = useRef<number | null>(null)

  const { bars, isLoading, isError } = useMiniKlineData(symbol, timeframe)

  // CHANGE-20260715-006: 每次 render 同步 refs（在 effects 之前，确保 effect 内读到最新值）
  barsLengthRef.current = bars.length
  timeframeRef.current = timeframe

  // CHANGE-20260715-006: 稳定的 applyViewportRange——从 refs 读取最新值
  // useCallback 空依赖：函数引用稳定，ResizeObserver 可安全持有
  const applyViewportRange = useCallback((width: number) => {
    const chart = chartRef.current
    if (!chart) return
    const vp = computeMiniKlineViewport(barsLengthRef.current, timeframeRef.current, width)
    if (vp.visibleBars <= 0) return
    chart.timeScale().setVisibleLogicalRange({
      from: vp.from,
      to: vp.to,
    })
  }, [])

  // CHANGE-20260715-006: 稳定的 scheduleApplyRange——取消上一个 rAF 后调度新 rAF
  // 避免快速切换 symbol/timeframe 时 stale rAF 覆盖新 range
  const scheduleApplyRange = useCallback((width: number) => {
    if (rafIdRef.current !== null) {
      cancelAnimationFrame(rafIdRef.current)
      rafIdRef.current = null
    }
    rafIdRef.current = requestAnimationFrame(() => {
      rafIdRef.current = null
      applyViewportRange(width)
    })
  }, [applyViewportRange])

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
        // CHANGE-20260715-002: 价格轴最小宽度 56px（与 viewport 计算的 priceScaleWidth 对齐）
        minimumWidth: MIN_PRICE_SCALE_WIDTH,
        // CHANGE-20260715-002: autoScale + scaleMargins {top:0.08, bottom:0.08}
        autoScale: true,
        scaleMargins: {
          top: 0.08,
          bottom: 0.08,
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
      // CHANGE-20260715-002: autoscaleInfoProvider 扩展价格范围
      // 在默认可见 priceRange 基础上扩展：上方 12%，下方 15%
      // 使用 computeAutoscaleRange 纯函数确保逻辑可测试
      // lightweight-charts v4: autoscaleInfoProvider 是 SeriesOptions 的一部分（不是 ISeriesApi 方法）
      autoscaleInfoProvider: (baseImpl: () => AutoscaleInfo | null) => {
        const base = baseImpl()
        if (!base || !base.priceRange) return base ?? null
        const { minValue, maxValue } = base.priceRange
        const extended = computeAutoscaleRange(minValue, maxValue)
        if (!extended) return base
        return {
          ...base,
          priceRange: {
            minValue: extended.min,
            maxValue: extended.max,
          },
        }
      },
    })

    seriesRef.current = series

    // CHANGE-20260715-006: ResizeObserver 调用稳定的 scheduleApplyRange
    // 不再闭包捕获 bars/timeframe（旧代码捕获首次 render 闭包，宽度变化时 range 计算错误）
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        // CHANGE-20260715-002: 使用整数 contentRect.width 避免亚像素抖动
        const intWidth = Math.floor(entry.contentRect.width)
        if (intWidth <= 0) continue
        const chart = chartRef.current
        if (!chart) continue
        chart.applyOptions({ width: intWidth })
        containerWidthRef.current = intWidth
        // 宽度变化时在 rAF 中重新应用 range（避免在 ResizeObserver 回调中直接操作引发布局抖动）
        scheduleApplyRange(intWidth)
      }
    })
    resizeObserver.observe(containerRef.current)

    // 初始化 containerWidthRef
    containerWidthRef.current = Math.floor(containerRef.current.clientWidth)

    return () => {
      // CHANGE-20260715-006: 卸载时取消 pending rAF
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current)
        rafIdRef.current = null
      }
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      containerWidthRef.current = 0
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

    // CHANGE-20260715-006: setData 后在 rAF 中执行 setVisibleLogicalRange
    // 不调用 fitContent（避免先全屏再设 range 的竞态）
    // 使用 scheduleApplyRange 取消上一个 pending rAF，避免快速切周期时 stale rAF 覆盖
    const width = containerWidthRef.current || Math.floor(containerRef.current?.clientWidth ?? 0)
    if (width > 0) {
      scheduleApplyRange(width)
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
