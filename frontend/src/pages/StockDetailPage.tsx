// 个股详情页（受保护路由，动态参数 :symbol）
// 对应原型：stock-detail.html (V1.6.3)
// 图表工作台核心页面：以 K 线图及图上策略可视化为核心
//
// 用法：路由 /stock/:symbol?source=watchlist&strategy=node&view=current
//   - source: selection（选股结果）/ watchlist（自选监控），默认 watchlist
//   - strategy: 策略标识（dsa/breakout/node/atr/volume/combined），默认 node
//   - view: snapshot（命中时点冻结证据）/ current（当前监控状态），默认 current
//
// V1.6.3 精简：无"策略当前计算结果"模块、无"事件时间轴"模块

import { useState, useMemo } from 'react'
import { useParams, useSearchParams, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import StrategyChart from '@/components/StrategyChart'
import type { BarData, ChartEvent } from '@/components/StrategyChart'
import {
  useInstrumentBySymbol,
  useBars,
  useInstrumentEvents,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useWatchlist,
} from '@/hooks/useApi'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { useToast } from '@/store/toast'

// 绘图工具定义（对齐原型 .tv-drawing-tools）
const DRAWING_TOOLS = [
  { id: 'crosshair', title: '十字光标', icon: '＋' },
  { id: 'trendline', title: '趋势线', icon: '╱' },
  { id: 'horizontalline', title: '水平线', icon: '—' },
  { id: 'rectangle', title: '矩形', icon: '□' },
  { id: 'text', title: '文字', icon: 'T' },
  { id: 'measure', title: '测量', icon: '↔' },
] as const

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
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // 解析 URL 参数
  const source = (searchParams.get('source') || 'watchlist') as 'selection' | 'watchlist'
  const strategy = searchParams.get('strategy') || 'node'
  const view = (searchParams.get('view') || 'current') as 'snapshot' | 'current'

  // 根据 source + strategy 调用 manifest.resolveStrategy 确定默认图层集
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // 本地状态：当前激活的绘图工具
  const [activeTool, setActiveTool] = useState<string>('crosshair')
  // 本地状态：当前周期（由 StrategyChart 工具栏联动）
  const [timeframe, setTimeframe] = useState<string>('1d')

  // 数据查询：股票基本信息
  const instrumentQuery = useInstrumentBySymbol(symbol)
  const instrumentId = instrumentQuery.data?.id

  // 数据查询：K 线行情（依赖 instrumentId，前复权，拉取足够数量的 bar 供指标计算）
  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: 500,
  })

  // 数据查询：策略事件
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 数据查询：自选列表（用于判断当前股票是否已在自选）
  const watchlistQuery = useWatchlist()

  // 自选变更操作
  const addWatchlist = useAddToWatchlist()
  const removeWatchlist = useRemoveFromWatchlist()

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

  // 最新报价（使用原始 Bar 数据以获取 amount 字段）
  const lastBar = barsQuery.data?.items?.[barsQuery.data.items.length - 1] || null
  const prevBar = barsQuery.data?.items?.[barsQuery.data.items.length - 2] || null
  const changePercent = lastBar && prevBar
    ? ((lastBar.close - prevBar.close) / prevBar.close * 100)
    : 0
  const isUp = changePercent >= 0

  // 加载状态：股票信息加载中
  const isInstrumentLoading = instrumentQuery.isLoading
  // 行情数据加载中（首次加载且无缓存数据）
  const isBarsLoading = !!instrumentId && barsQuery.isLoading && bars.length === 0

  // 来源徽章与返回链接
  const sourceBadge = source === 'selection' ? '选股结果' : '自选监控'
  const backPath = source === 'selection' ? '/screener' : '/watchlist'

  // 操作：保存图表布局
  const handleSaveLayout = () => {
    showToast('操作完成', '已保存图表布局')
  }

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

  // 操作：切换视图上下文（snapshot / current）
  const handleSwitchView = (nextView: 'snapshot' | 'current') => {
    const next = new URLSearchParams(searchParams)
    next.set('view', nextView)
    setSearchParams(next, { replace: true })
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
          <aside className="tv-drawing-tools" aria-label="绘图工具" />
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
    <div className="tv-content">
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
            <b className={isUp ? 'pos' : 'neg'}>{lastBar ? lastBar.close.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>涨跌</span>
            <b className={isUp ? 'pos' : 'neg'}>
              {lastBar && prevBar ? `${isUp ? '+' : ''}${changePercent.toFixed(2)}%` : '--'}
            </b>
          </div>
          <div>
            <span>开盘</span>
            <b>{lastBar ? lastBar.open.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最高</span>
            <b>{lastBar ? lastBar.high.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最低</span>
            <b>{lastBar ? lastBar.low.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>成交额</span>
            <b>{lastBar ? formatAmount(lastBar.amount) : '--'}</b>
          </div>
        </div>
        {/* 操作：视图切换 + 保存布局 + 加入/移出自选 */}
        <div className="actions">
          <div className="context-tabs">
            <button
              className={clsx('context-tab', view === 'current' && 'active')}
              onClick={() => handleSwitchView('current')}
              title="当前监控状态"
            >当前监控</button>
            <button
              className={clsx('context-tab', view === 'snapshot' && 'active')}
              onClick={() => handleSwitchView('snapshot')}
              title="命中时点冻结证据"
            >命中时点</button>
          </div>
          <button className="btn" onClick={handleSaveLayout}>保存布局</button>
          <button
            className={clsx('btn', inWatchlist ? 'danger' : 'primary')}
            onClick={handleToggleWatchlist}
            disabled={!instrumentId || addWatchlist.isPending || removeWatchlist.isPending}
          >
            {inWatchlist ? '移出自选' : '加入自选'}
          </button>
        </div>
      </div>

      {/* ===== 工作区：两列布局（38px 绘图工具列 + 1fr 图表列）===== */}
      <div className="tv-workspace">
        {/* 左侧绘图工具 */}
        <aside className="tv-drawing-tools" aria-label="绘图工具">
          {DRAWING_TOOLS.map((tool) => (
            <button
              key={tool.id}
              title={tool.title}
              className={clsx(activeTool === tool.id && 'active')}
              onClick={() => setActiveTool(tool.id)}
            >{tool.icon}</button>
          ))}
          <div className="tv-tool-sep"></div>
          <button
            title="删除绘图"
            onClick={() => showToast('操作完成', '已清除所有绘图')}
          >⌫</button>
        </aside>

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
                strategyId={strategyDef.id}
                source={source}
                view={view}
                height={655}
                timeframe={timeframe}
                onTimeframeChange={setTimeframe}
              />
              {/* 状态栏：行情延迟/复权/时区/策略计算时间 */}
              <div className="tv-chart-status">
                <span><i className="dot ok"></i>行情延迟 0.8s</span>
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
