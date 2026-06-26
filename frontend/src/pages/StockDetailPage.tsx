// 个股详情页（受保护路由，动态参数 :symbol）
// 对应原型：stock-detail.html (V1.6.3)
// 图表工作台核心页面：以 K 线图及图上策略可视化为核心
//
// 用法：路由 /stock/:symbol?source=watchlist&strategy=node&capture=feishu
//   - source: selection（选股结果）/ watchlist（自选监控），默认 watchlist
//   - strategy: 策略标识（dsa/breakout/node/atr/volume/combined），默认 node
//   - capture: feishu 时进入截图模式，隐藏侧栏与用户信息，并暴露 data-render-ready 属性
//
// V1.6.3 精简：无"策略当前计算结果"模块、无"事件时间轴"模块

import { useState, useMemo, useRef, useEffect, useCallback } from 'react'
import { useParams, useSearchParams, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import StrategyChart from '@/components/StrategyChart'
import type { BarData, ChartEvent } from '@/components/StrategyChart'
import type { ChartViewport } from '@/components/chartViewport'
import type { IndicatorResponse } from '@/api/endpoints'
import { formatShanghaiTimeShort } from '@/utils/datetime'
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
  useNotificationChannels,
} from '@/hooks/useApi'
import { useMutation } from '@tanstack/react-query'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { useToast } from '@/store/toast'
import { useAuthStore } from '@/store/auth'
import { sendStockDetailFeishu } from '@/api/endpoints'
import type { StockDetailFeishuResponse } from '@/api/endpoints'

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
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR
  const isCaptureMode = searchParams.get('capture') === 'feishu'

  // 根据 source + strategy 调用 manifest.resolveStrategy 确定默认图层集
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // 本地状态：当前周期（由 StrategyChart 工具栏联动）
  const [timeframe, setTimeframe] = useState<string>('1d')
  // [chartViewport] - 每个周期独立保存 viewport，切换周期时重置目标周期 viewport
  //   key=timeframe, value=ChartViewport（未保存的周期由 StrategyChart 内部计算默认值）
  //   避免 15m/1h/1w/1mo 切换时 viewport 串台（advice.md 第三节问题 3）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})
  // 全屏查看容器
  const containerRef = useRef<HTMLDivElement>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)

  // [chartViewport] - viewport 变化回调：更新当前周期的 viewport（按周期独立保存）
  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe(prev => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // [chartViewport] - 周期切换：清空目标周期的 viewport 记录，
  //   让 StrategyChart 回退到默认末尾视区（advice.md 第三节问题 3）
  const handleTimeframeChange = useCallback((tf: string) => {
    setViewportByTimeframe(prev => {
      if (!(tf in prev)) return prev  // 目标周期未保存，无需清空
      const next = { ...prev }
      delete next[tf]
      return next
    })
    setTimeframe(tf)
  }, [])

  // 数据查询：股票基本信息
  const instrumentQuery = useInstrumentBySymbol(symbol)
  const instrumentId = instrumentQuery.data?.id

  // admin 权限判断 + 通知渠道查询（仅 admin 可见「发送到飞书」按钮）
  const user = useAuthStore((s) => s.user)
  const isAdmin = user?.role === 'admin'
  // [capture-mode] 截图模式下禁用 admin API（通知渠道列表）：
  // capture token 无 admin 角色，调用会 401 触发 axios 拦截器跳转登录页，
  // 导致 StockDetailPage 卸载、data-render-ready 永远 false、截图超时 502
  const channelsQuery = useNotificationChannels(!isCaptureMode)
  const activeChannels = (channelsQuery.data?.items ?? []).filter((c) => c.status === 'active')

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

  // 发送到飞书（admin only） - 复用监控链路，返回 4 个分步骤布尔结果
  const [feishuOpen, setFeishuOpen] = useState(false)
  const [feishuResult, setFeishuResult] = useState<StockDetailFeishuResponse | null>(null)
  const [selectedChannelId, setSelectedChannelId] = useState<string>('')
  const sendFeishuMutation = useMutation<
    StockDetailFeishuResponse,
    Error,
    { instrId: string; channelId: string }
  >({
    mutationFn: ({ instrId, channelId }) => sendStockDetailFeishu(instrId, channelId),
  })

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

  // 截图模式：股票信息 + K线 + 指标加载成功后标记可渲染
  // [advice.md] 事件历史加载失败不应阻止截图（事件仅用于图表标注，非截图必要条件）
  // 历史问题：eventsQuery.isSuccess 必填导致事件接口超时时 data-render-ready 永远为 false，
  // capture worker 等待 30s 超时返回 502，图片无法投递
  const isRenderReady =
    isCaptureMode &&
    instrumentQuery.isSuccess &&
    barsQuery.isSuccess &&
    indicatorsQuery.isSuccess

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

  // 发送到飞书（admin only） - 调用后端复用监控链路，根据 4 布尔结果显示 toast
  const handleSendFeishu = (channelId: string) => {
    if (!instrumentId) return
    setFeishuResult(null)
    sendFeishuMutation.mutate(
      { instrId: instrumentId, channelId },
      {
        onSuccess: (res) => {
          setFeishuResult(res)
          if (res.text_ok && res.screenshot_ok && res.image_upload_ok && res.feishu_send_ok) {
            showToast('发送成功', '文本+图片已发送到飞书')
          } else {
            const steps = [
              res.text_ok ? '文本\u2713' : '文本\u2717',
              res.screenshot_ok ? '截图\u2713' : '截图\u2717',
              res.image_upload_ok ? '图片拉取\u2713' : '图片拉取\u2717',
              res.feishu_send_ok ? '飞书发送\u2713' : '飞书发送\u2717',
            ]
            showToast('部分失败', steps.join(' \u00b7 '))
          }
        },
        onError: () => showToast('发送失败', '请重试'),
      },
    )
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
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
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
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
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
    <div
      className="tv-content"
      ref={containerRef}
      data-testid="stock-detail-capture"
      data-render-ready={isRenderReady ? 'true' : 'false'}
    >
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
          {isAdmin && (
            <button
              className="btn small"
              onClick={() => {
                setFeishuResult(null)
                setSelectedChannelId(activeChannels[0]?.id ?? '')
                setFeishuOpen(true)
              }}
              disabled={!instrumentId}
            >
              发送到飞书
            </button>
          )}
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

      {/* 发送到飞书模态框（admin only） - 选择渠道 + 显示 4 布尔结果 */}
      {feishuOpen && (
        <div
          className="modal-backdrop open"
          onClick={() => !sendFeishuMutation.isPending && setFeishuOpen(false)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>发送到飞书 - {inst.name}</h3>
              <button
                className="icon-btn"
                onClick={() => !sendFeishuMutation.isPending && setFeishuOpen(false)}
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              {activeChannels.length === 0 ? (
                <p style={{ color: '#888', padding: '12px 0' }}>
                  暂无 active 通知渠道，请先在通知渠道页面创建并启用一个飞书渠道。
                </p>
              ) : (
                <>
                  <p style={{ marginBottom: 8, fontSize: 13, color: '#666' }}>选择目标渠道：</p>
                  <select
                    className="memo-textarea"
                    style={{ height: 'auto', padding: '8px', marginBottom: 12 }}
                    value={selectedChannelId}
                    onChange={(e) => setSelectedChannelId(e.target.value)}
                  >
                    {activeChannels.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.display_name}（{c.adapter_type}）
                      </option>
                    ))}
                  </select>

                  {feishuResult && (
                    <div
                      style={{
                        padding: '12px',
                        background: '#f5f5f5',
                        borderRadius: 4,
                        fontSize: 13,
                      }}
                    >
                      <div style={{ marginBottom: 4 }}>
                        文本投递:{' '}
                        <b style={{ color: feishuResult.text_ok ? '#52c41a' : '#ff4d4f' }}>
                          {feishuResult.text_ok ? '成功' : '失败'}
                        </b>
                      </div>
                      <div style={{ marginBottom: 4 }}>
                        截图:{' '}
                        <b style={{ color: feishuResult.screenshot_ok ? '#52c41a' : '#ff4d4f' }}>
                          {feishuResult.screenshot_ok ? '成功' : '失败'}
                        </b>
                      </div>
                      <div style={{ marginBottom: 4 }}>
                        图片拉取:{' '}
                        <b
                          style={{
                            color: feishuResult.image_upload_ok ? '#52c41a' : '#ff4d4f',
                          }}
                        >
                          {feishuResult.image_upload_ok ? '成功' : '失败'}
                        </b>
                      </div>
                      <div>
                        飞书发送:{' '}
                        <b style={{ color: feishuResult.feishu_send_ok ? '#52c41a' : '#ff4d4f' }}>
                          {feishuResult.feishu_send_ok ? '成功' : '失败'}
                        </b>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
            <div className="modal-foot">
              <button
                className="btn primary"
                disabled={
                  activeChannels.length === 0 ||
                  sendFeishuMutation.isPending ||
                  !instrumentId ||
                  !selectedChannelId
                }
                onClick={() => {
                  if (selectedChannelId) handleSendFeishu(selectedChannelId)
                }}
              >
                {sendFeishuMutation.isPending ? '发送中...' : '发送'}
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
                onTimeframeChange={handleTimeframeChange}
                viewport={viewportByTimeframe[timeframe]}
                onViewportChange={handleViewportChange}
              />
              {/* 状态栏：行情延迟/复权/时区/策略计算时间 */}
              <div className="tv-chart-status">
                <span><i className={quoteQuery.data ? 'dot ok' : 'dot warn'}></i>{quoteQuery.data ? `行情延迟 ${((Date.now() - quoteQuery.dataUpdatedAt) / 1000).toFixed(0)}s` : '行情延迟 --'}</span>
                <span>复权：前复权</span>
                <span>时区：Asia/Shanghai</span>
                <span>策略计算：{formatShanghaiTimeShort(new Date())}</span>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
