// CHANGE-20260713-010: 右栏小 K 线卡片
// CHANGE-20260714-001: 扩展为五周期（15m/60m/日/周/月），删除标题，移除图内 TV 标志
// CHANGE-20260715-002 P0: 重写 viewport 计算 + autoscaleInfoProvider
// CHANGE-20260715-006: 根治 ResizeObserver/rAF 闭包问题
// CHANGE-20260715-007: 单一 viewport 重写——setData 只传最后 visibleBars 根，
//   负 logical range 成为真实空白；提取 miniKlineController 使图表逻辑可行为测试。
import { useEffect, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import { useMiniKlineData, type MiniKlineTimeframe } from './useMiniKlineData'
import { createMiniKlineController, type MiniKlineController } from './miniKlineController'

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

export function MiniKlineCard({ symbol }: MiniKlineCardProps) {
  const [timeframe, setTimeframe] = useState<MiniKlineTimeframe>('1d')
  const containerRef = useRef<HTMLDivElement>(null)
  const controllerRef = useRef<MiniKlineController | null>(null)
  const containerWidthRef = useRef<number>(0)

  const { bars, isLoading, isError } = useMiniKlineData(symbol, timeframe)

  // 创建 chart controller（仅一次）
  useEffect(() => {
    if (!containerRef.current) return
    const initialWidth = Math.floor(containerRef.current.clientWidth)
    containerWidthRef.current = initialWidth

    const controller = createMiniKlineController(
      containerRef.current,
      initialWidth,
      {
        // 轻量图表 v4 createChart 与 controller 内部接口结构兼容
        createChart: createChart as unknown as Parameters<typeof createMiniKlineController>[2]['createChart'],
        raf: {
          request: (cb: () => void) => requestAnimationFrame(cb),
          cancel: (id: number) => cancelAnimationFrame(id),
        },
      },
    )
    controllerRef.current = controller

    // ResizeObserver：宽度变化时取消旧 rAF + 重新 setData + range
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const intWidth = Math.floor(entry.contentRect.width)
        if (intWidth <= 0) continue
        containerWidthRef.current = intWidth
        controller.resize(intWidth)
      }
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      resizeObserver.disconnect()
      controller.destroy()
      controllerRef.current = null
      containerWidthRef.current = 0
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 数据/周期变化 → 重新 setData + range
  useEffect(() => {
    const controller = controllerRef.current
    if (!controller) return
    const width = containerWidthRef.current || Math.floor(containerRef.current?.clientWidth ?? 0)
    controller.setData(bars, timeframe, width)
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
