// 个股详情页（受保护路由，动态参数 :symbol）
// 对应原型：stock-detail.html (V1.6.3)
// 图表工作台核心页面：以 K 线图及图上策略可视化为核心
//
// 用法：路由 /stock/:symbol?source=watchlist&strategy=node
//   - source: selection（选股结果）/ watchlist（自选监控），默认 watchlist
//   - strategy: 策略标识（dsa/breakout/node/atr/volume/combined），默认 node
//
// V1.6.3 精简：无"策略当前计算结果"模块、无"事件时间轴"模块

import { useState, useMemo, useRef, useEffect } from 'react'
import { useParams, useSearchParams, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import StrategyChart from '@/components/StrategyChart'
import type { BarData, ChartEvent } from '@/components/StrategyChart'
import type { IndicatorResponse } from '@/api/endpoints'
import {
  useInstrumentBySymbol,
  useBars,
  useIndicators,
  useInstrumentEvents,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useWatchlist,
  useBatchInstruments,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  useRealtimeQuote,
} from '@/hooks/useApi'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { useToast } from '@/store/toast'

// 市场代码 -> 中文标签映射
const MARKET_LABELS: Record<string, string> = {
  A_SHARE: 'A股',
  STAR: '科创板',
  MAIN: '主板',
  SME: '中小板',
  GEM: '创业板',
  BSE: '北交所',
}

// 格式化成交额（元 -> 亿/万）
function formatAmount(v: number): string {
  if (!v || v <= 0) return '--'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(0)
}

export default function StockDetailPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // 解析 URL 参数
  const source = (searchParams.get('source') || 'watchlist') as 'selection' | 'watchlist'
  const strategy = searchParams.get('strategy') || 'node'

  // 根据 source + strategy 调用 manifest.resolveStrategy 确定默认图层集
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // 本地状态：当前周期（由 StrategyChart 工具栏联动）
  const [timeframe, setTimeframe] = useState<string>('1d')
  // 全屏查看容器
  const containerRef = useRef<HTMLDivElement>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)

  // 数据查询：股票基本信息
  const instrumentQuery = useInstrumentBySymbol(symbol)
  const instrumentId = instrumentQuery.data?.id

  // 数据查询：K 线行情（依赖 instrumentId，前复权，与 indicators 的 bars=250 对齐避免数据范围不匹配）
  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: 250,
  })

  // 数据查询：策略图表指标（依赖 instrumentId，与 bars 同 timeframe/adj，拉取最近 250 根 bar 的指标）
  const indicatorsQuery = useIndicators(instrumentId, {
    timeframe,
    adj: 'qfq',
    bars: 250,
  })
  const indicators: IndicatorResponse | undefined = indicatorsQuery.data

  // 数据查询：实时报价（交易时段内 10s 轮询）
  const quoteQuery = useRealtimeQuote(instrumentId)

  // 数据查询：策略事件
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 数据查询：自选列表（用于判断当前股票是否已在自选）
  const watchlistQuery = useWatchlist()

  // 批量查询自选对应的股票信息，用于将 instrument_id 映射为 symbol 以支持上下切换
  const watchlistInstrumentIds = useMemo(
    () => watchlistQuery.data?.items.map((item) => item.instrument_id) ?? [],
    [watchlistQuery.data],
  )
  const batchInstrumentsQuery = useBatchInstruments(watchlistInstrumentIds)
  const instrumentSymbolMap = useMemo(() => {
    const map = new Map<string, string>()
    if (!batchInstrumentsQuery.data?.items) return map
    for (const inst of batchInstrumentsQuery.data.items) {
      map.set(inst.id, inst.symbol)
    }
    return map
  }, [batchInstrumentsQuery.data])

  // 自选变更操作
  const addWatchlist = useAddToWatchlist()
  const removeWatchlist = useRemoveFromWatchlist()

  // 备忘录
  const [memoOpen, setMemoOpen] = useState(false)
  const [memoContent, setMemoContent] = useState('')
  const [memoNotify, setMemoNotify] = useState(false)
  const stockMemoQuery = useStockMemo(instrumentId)
  const upsertMemo = useUpsertStockMemo()
  const deleteMemo = useDeleteStockMemo()

  useEffect(() => {
    if (stockMemoQuery.data) {
      setMemoContent(stockMemoQuery.data.content)
      setMemoNotify(stockMemoQuery.data.notify_feishu)
    } else {
      setMemoContent('')
      setMemoNotify(false)
    }
  }, [stockMemoQuery.data])

  // 判断当前股票是否已在自选（active=true）
  const inWatchlist = useMemo(() => {
    if (!instrumentId || !watchlistQuery.data) return false
    return watchlistQuery.data.items.some(
      (item) => item.instrument_id === instrumentId && item.active,
    )
  }, [instrumentId, watchlistQuery.data])

  // 转换 Bar 数据为 StrategyChart 需要的 BarData 格式
  const bars: BarData[] = useMemo(() => {
    if (!barsQuery.data?.items) return []
    return barsQuery.data.items.map((b) => ({
      time: b.trade_time || b.trade_date || '',
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
    }))
  }, [barsQuery.data])

  // 转换策略事件为 StrategyChart 需要的 ChartEvent 格式
  const events: ChartEvent[] = useMemo(() => {
    if (!eventsQuery.data?.items) return []
    return eventsQuery.data.items.map((e) => ({
      time: e.event_time,
      type: e.event_type,
      title: (e.payload?.title as string) || e.event_type,
      description: e.payload?.description as string | undefined,
    }))
  }, [eventsQuery.data])

  // 最新报价（优先使用实时报价，降级到 barsQuery 最后一根 bar）
  const quote = quoteQuery.data
  const lastBar = barsQuery.data?.items?.[barsQuery.data.items.length - 1] || null
  const currentPrice = quote?.current_price ?? lastBar?.close ?? null
  const openPrice = quote?.open ?? lastBar?.open ?? null
  const highPrice = quote?.high ?? lastBar?.high ?? null
  const lowPrice = quote?.low ?? lastBar?.low ?? null
  const amountValue = quote?.amount ?? lastBar?.amount ?? null
  const changePercent = quote?.change_pct ?? (lastBar && barsQuery.data?.items?.[barsQuery.data.items.length - 2]
    ? ((lastBar.close - barsQuery.data.items[barsQuery.data.items.length - 2].close) / barsQuery.data.items[barsQuery.data.items.length - 2].close * 100)
    : null)
  const isUp = changePercent !== null ? changePercent >= 0 : true

  // 加载状态：股票信息加载中
  const isInstrumentLoading = instrumentQuery.isLoading
  // 行情数据加载中（首次加载且无缓存数据）
  const isBarsLoading = !!instrumentId && barsQuery.isLoading && bars.length === 0

  // 来源徽章与返回链接
  const sourceBadge = source === 'selection' ? '选股结果' : '自选监控'
  const backPath = source === 'selection' ? '/screener' : '/watchlist'

  // 操作：加入/移出自选
  const handleToggleWatchlist = () => {
    if (!instrumentId) return
    if (inWatchlist) {
      removeWatchlist.mutate(instrumentId, {
        onSuccess: () => showToast('操作完成', '已移出自选'),
      })
    } else {
      addWatchlist.mutate(
        { instrument_id: instrumentId, source },
        { onSuccess: () => showToast('操作完成', '已加入自选') },
      )
    }
  }

  // 全屏查看
  const handleFullscreen = () => {
    if (!document.fullscreenElement) {
      containerRef.current?.requestFullscreen().catch(() => {})
    } else {
      document.exitFullscreen().catch(() => {})
    }
  }
  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [])

  // 在自选列表中上下切换股票
  const watchlistItems = watchlistQuery.data?.items ?? []
  const currentIndex = instrumentId
    ? watchlistItems.findIndex((item) => item.instrument_id === instrumentId)
    : -1
  const canNavigate =
    watchlistItems.length >= 2 && currentIndex >= 0
  const navigateToStock = (direction: number) => {
    if (!canNavigate) return
    const nextIndex = (currentIndex + direction + watchlistItems.length) % watchlistItems.length
    const target = watchlistItems[nextIndex]
    const targetSymbol = instrumentSymbolMap.get(target.instrument_id)
    if (!targetSymbol) return
    navigate(`/stock/${targetSymbol}?source=watchlist&strategy=${strategy}`)
  }

  // 股票信息加载中
  if (isInstrumentLoading) {
    return (
      <div className="tv-content">
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={() => navigate(backPath)} title="返回">←</button>
            <div>
              <div className="tv-symbol-title">
                <span>加载中...</span>
                <span className="tv-code">{symbol || ''}</span>
              </div>
              <div className="tv-symbol-meta">正在获取股票数据</div>
            </div>
          </div>
        </div>
        <div className="tv-workspace">
          <section className="tv-chart-column">
            <div className="tv-chart-loading">行情数据加载中...</div>
          </section>
        </div>
      </div>
    )
  }

  // 股票不存在或查询出错
  if (!instrumentQuery.data) {
    return (
      <div className="tv-content">
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={() => navigate(backPath)} title="返回">←</button>
            <div>
              <div className="tv-symbol-title">
                <span>未找到股票</span>
                <span className="tv-code">{symbol || ''}</span>
              </div>
              <div className="tv-symbol-meta">
                {instrumentQuery.isError ? '股票信息查询失败，请稍后重试' : '请检查股票代码是否正确'}
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const inst = instrumentQuery.data
  // 元信息：市场 · 人民币 · 实时行情
  const metaParts = [
    MARKET_LABELS[inst.market] || inst.market,
    '人民币',
    '实时行情',
  ].filter(Boolean)

  return (
    <div className="tv-content" ref={containerRef}>
      {/* ===== 股票信息栏 ===== */}
      <div className="tv-symbol-bar">
        <div className="tv-symbol-left">
          <button className="icon-btn tv-back" onClick={() => navigate(backPath)} title="返回">←</button>
          <div>
            <div className="tv-symbol-title">
              <span>{inst.name}</span>
              <span className="tv-code">{inst.symbol}</span>
              <span className="status-pill ok">{sourceBadge}</span>
            </div>
            <div className="tv-symbol-meta">{metaParts.join(' · ')}</div>
          </div>
        </div>
        {/* 报价条：现价/涨跌/开盘/最高/最低/成交额 */}
        <div className="tv-quote-strip">
          <div>
            <span>现价</span>
            <b className={isUp ? 'pos' : 'neg'}>{currentPrice !== null ? currentPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>涨跌</span>
            <b className={isUp ? 'pos' : 'neg'}>
              {changePercent !== null ? `${isUp ? '+' : ''}${changePercent.toFixed(2)}%` : '--'}
            </b>
          </div>
          <div>
            <span>开盘</span>
            <b>{openPrice !== null ? openPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最高</span>
            <b>{highPrice !== null ? highPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最低</span>
            <b>{lowPrice !== null ? lowPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>成交额</span>
            <b>{amountValue !== null ? formatAmount(amountValue) : '--'}</b>
          </div>
        </div>
        {/* 操作：加入/移出自选、切换、全屏 */}
        <div className="actions">
          <button
            className={clsx('btn', inWatchlist ? 'danger' : 'primary')}
            onClick={handleToggleWatchlist}
            disabled={!instrumentId || addWatchlist.isPending || removeWatchlist.isPending}
          >
            {inWatchlist ? '移出自选' : '加入自选'}
          </button>
          <button className="btn small" onClick={() => navigateToStock(-1)} disabled={!canNavigate}>
            上一只
          </button>
          <button className="btn small" onClick={() => navigateToStock(1)} disabled={!canNavigate}>
            下一只
          </button>
          <button className="btn small" onClick={handleFullscreen}>
            {isFullscreen ? '退出全屏' : '全屏查看'}
          </button>
          <button className="btn small" onClick={() => setMemoOpen(true)}>
            备忘录
          </button>
        </div>
      </div>

      {/* 备忘录模态框 */}
      {memoOpen && (
        <div className="modal-backdrop open" onClick={() => setMemoOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>备忘录 - {inst.name}</h3>
              <button className="icon-btn" onClick={() => setMemoOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              <textarea
                className="memo-textarea"
                value={memoContent}
                onChange={(e) => setMemoContent(e.target.value)}
                placeholder="输入备忘录内容..."
                rows={6}
              />
              <label className="memo-switch">
                <input
                  type="checkbox"
                  checked={memoNotify}
                  onChange={(e) => setMemoNotify(e.target.checked)}
                />
                <span>盘中推送飞书</span>
              </label>
            </div>
            <div className="modal-foot">
              {stockMemoQuery.data && (
                <button
                  className="btn danger"
                  onClick={() => {
                    deleteMemo.mutate(instrumentId!, {
                      onSuccess: () => {
                        showToast('已删除', '备忘录已删除')
                        setMemoOpen(false)
                        setMemoContent('')
                        setMemoNotify(false)
                      },
                    })
                  }}
                  disabled={deleteMemo.isPending}
                >
                  删除
                </button>
              )}
              <button
                className="btn primary"
                onClick={() => {
                  if (!memoContent.trim()) {
                    showToast('提示', '备忘录内容不能为空')
                    return
                  }
                  upsertMemo.mutate(
                    { instrumentId: instrumentId!, payload: { content: memoContent, notify_feishu: memoNotify } },
                    {
                      onSuccess: () => {
                        showToast('已保存', '备忘录已保存')
                        setMemoOpen(false)
                      },
                      onError: () => showToast('保存失败', '请重试'),
                    },
                  )
                }}
                disabled={upsertMemo.isPending || !memoContent.trim()}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 工作区：单列表布（完整图表宽度） ===== */}
      <div className="tv-workspace">
        {/* 图表区 */}
        <section className="tv-chart-column">
          {isBarsLoading ? (
            <div className="tv-chart-loading">行情数据加载中...</div>
          ) : (
            <>
              {/* StrategyChart 内部渲染：工具栏 + 策略图示区 + 画布区 */}
              <StrategyChart
                symbol={inst.symbol}
                bars={bars}
                events={events}
                indicators={indicators}
                strategyId={strategyDef.id}
                source={source}
                height={655}
                timeframe={timeframe}
                onTimeframeChange={setTimeframe}
              />
              {/* 状态栏：行情延迟/复权/时区/策略计算时间 */}
              <div className="tv-chart-status">
                <span><i className={quoteQuery.data ? 'dot ok' : 'dot warn'}></i>{quoteQuery.data ? `行情延迟 ${((Date.now() - quoteQuery.dataUpdatedAt) / 1000).toFixed(0)}s` : '行情延迟 --'}</span>
                <span>复权：前复权</span>
                <span>时区：Asia/Shanghai</span>
                <span>策略计算：{new Date().toLocaleTimeString('zh-CN', { hour12: false })}</span>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
