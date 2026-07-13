// 个股详情页（受保护路由，动态参数 :symbol）
// 对应原型：stock-detail.html (V1.6.3)
// 图表工作台核心页面：以 K 线图及图上策略可视化为核心
//
// 用法：路由 /stock/:symbol?source=watchlist&strategy=node&capture=feishu
//   - source: selection（选股结果）/ watchlist（自选监控），默认 watchlist
//   - strategy: 策略标识（dsa/breakout/node/atr/volume/combined），默认 node
//   - capture: feishu 时进入截图模式，隐藏侧栏与用户信息，并暴露 data-render-ready 属性
//
// 阶段四重构：/market 和 /stock/:symbol 共用 useStockResearchData + StockResearchWorkspace。
// 详情页专属能力（自选操作、上下切换、memo、飞书）拆到 useStockDetailActions / useStockDetailFeishu。
// 本页面降为路由适配器：解析 URL → 调用共享 hooks → 渲染 header + StockResearchWorkspace + 结构面板 + modals。

import { useState, useCallback, useEffect, useRef } from 'react'
import { useParams, useSearchParams, useNavigate, useLocation } from 'react-router-dom'
import clsx from 'clsx'
import { EventStatePanel } from '@/features/research-context/EventStatePanel'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { useStockDetailActions } from '@/features/stock-research/useStockDetailActions'
import { useStockDetailFeishu } from '@/features/stock-research/useStockDetailFeishu'
import {
  type DisplayTimeframe,
  normalizeDisplayTimeframe,
  normalizeResearchSource,
} from '@/features/stock-research/stockResearchTypes'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { MARKET_LABELS, formatAmount } from '@/utils/market'
import { resolveBackPath } from './detailNavigation'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { useToast } from '@/store/toast'

export default function StockDetailPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const showToast = useToast((s) => s.show)

  // 解析 URL 参数
  const source = normalizeResearchSource(searchParams.get('source'))
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR
  const isCaptureMode = searchParams.get('capture') === 'feishu'
  // [结构状态隐藏开关] - hideStructuralState=1 / capture=1 / capture=feishu 强制隐藏面板
  const hideStructuralStateParam =
    searchParams.get('hideStructuralState') === '1' ||
    searchParams.get('capture') === '1' ||
    isCaptureMode

  // [事件面板开关] - 首次默认收起，localStorage 持久化用户选择；capture 强制隐藏
  // P0-4: showStructuralState → eventPanelCollapsed（语义：true=收起）
  // localStorage key: panji:event-panel:v1
  const [eventPanelCollapsed, setEventPanelCollapsed] = useState<boolean>(() => {
    if (hideStructuralStateParam) return true
    const saved = localStorage.getItem('panji:event-panel:v1')
    return saved === null ? true : saved === 'collapsed'
  })
  const toggleEventPanel = useCallback(() => {
    if (hideStructuralStateParam) return
    setEventPanelCollapsed(prev => {
      const next = !prev
      localStorage.setItem('panji:event-panel:v1', next ? 'collapsed' : 'expanded')
      return next
    })
  }, [hideStructuralStateParam])
  const shouldShowPanel = !eventPanelCollapsed && !hideStructuralStateParam

  // timeframe：从 URL 解析（单一真源），工具栏切换写回 URL
  const timeframe: DisplayTimeframe = normalizeDisplayTimeframe(searchParams.get('timeframe'))

  // 工具栏切换周期：写回 URL（保留 source/strategy/capture 等其他参数）
  const handleTimeframeChange = useCallback((newTimeframe: DisplayTimeframe) => {
    const next = new URLSearchParams(searchParams)
    next.set('timeframe', newTimeframe)
    setSearchParams(next, { replace: false })
  }, [searchParams, setSearchParams])

  // 全屏查看容器
  const containerRef = useRef<HTMLDivElement>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const handleFullscreen = useCallback(() => {
    if (!document.fullscreenElement) {
      containerRef.current?.requestFullscreen().catch(() => {})
    } else {
      document.exitFullscreen().catch(() => {})
    }
  }, [])
  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [])

  // 共享研究数据 hook（/market 和 /stock 共用，只含核心查询）
  const researchData = useStockResearchData({ symbol: symbol ?? null, timeframe })
  const instrumentId = researchData.instrumentId

  // 详情页专属 actions（自选/上下切换/memo + returnTo 上下文恢复左栏列表）
  const returnToParam = searchParams.get('returnTo')
  const detailActions = useStockDetailActions({
    instrumentId,
    symbol,
    source,
    strategy,
    returnTo: returnToParam,
  })

  // 详情页专属飞书投递
  const feishu = useStockDetailFeishu({ instrumentId })

  // 来源徽章：根据 sourceListKind 显示"行情来源/自选来源/选股结果"
  // P0-4: 不能从 market 进入却显示"自选监控"
  // CHANGE-20260713-009: sourceListKind=market → "行情来源"（来自 /market?scope=market）
  // sourceListKind=watchlist + source=selection → "选股结果"（来自 /screener）
  // sourceListKind=watchlist + source=watchlist → "自选来源"（来自 /market?scope=watchlist 或直接访问）
  const sourceBadge = detailActions.sourceListKind === 'market'
    ? '行情来源'
    : (source === 'selection' ? '选股结果' : '自选来源')

  /** 统一返回按钮：优先使用 URL returnTo 参数，其次导航 state，否则按 source fallback */
  const handleBack = useCallback(() => {
    const returnFromUrl = searchParams.get('returnTo')
    const returnFromState = (location.state as { returnTo?: string } | undefined)?.returnTo
    navigate(resolveBackPath(returnFromUrl || returnFromState, source))
  }, [searchParams, location.state, navigate, source])

  // 备忘录保存
  const handleSaveMemo = useCallback(() => {
    if (!detailActions.memoContent.trim()) {
      showToast('提示', '备忘录内容不能为空')
      return
    }
    if (!instrumentId) return
    detailActions.upsertMemo.mutate(
      { instrumentId, payload: { content: detailActions.memoContent, notify_feishu: detailActions.memoNotify } },
      {
        onSuccess: () => {
          showToast('已保存', '备忘录已保存')
          detailActions.setMemoOpen(false)
        },
        onError: () => showToast('保存失败', '请重试'),
      },
    )
  }, [detailActions, instrumentId, showToast])

  // 备忘录删除
  const handleDeleteMemo = useCallback(() => {
    if (!instrumentId) return
    detailActions.deleteMemo.mutate(instrumentId, {
      onSuccess: () => {
        showToast('已删除', '备忘录已删除')
        detailActions.setMemoOpen(false)
        detailActions.setMemoContent('')
        detailActions.setMemoNotify(false)
      },
    })
  }, [detailActions, instrumentId, showToast])

  // 股票信息加载中
  if (researchData.instrumentQuery.isLoading) {
    return (
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
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
  if (!researchData.instrumentQuery.data) {
    return (
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
            <div>
              <div className="tv-symbol-title">
                <span>未找到股票</span>
                <span className="tv-code">{symbol || ''}</span>
              </div>
              <div className="tv-symbol-meta">
                {researchData.instrumentQuery.isError ? '股票信息查询失败，请稍后重试' : '请检查股票代码是否正确'}
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const inst = researchData.instrumentQuery.data
  const { priceSummary, quoteStatus, barsStatus } = researchData
  const quote = researchData.quoteQuery.data

  // [QuoteTrust] - 元信息：市场 · 人民币 · 行情状态 · update_time · K线状态
  const metaParts = [
    MARKET_LABELS[inst.market] || inst.market,
    '人民币',
    quoteStatus.label,
    quote?.update_time ? `更新 ${formatShanghaiTimeShort(quote.update_time)}` : null,
    barsStatus ? barsStatus.label : null,
  ].filter(Boolean)

  // 右栏事件状态面板（PRD V1.1: 使用 EventStatePanel，与 market 共用 query key）
  const eventStatePanel = shouldShowPanel && symbol ? (
    <aside className="tv-side-column">
      <EventStatePanel symbol={symbol} />
    </aside>
  ) : null

  // 事件面板开关 toolbar（渲染在图表上方）
  const structuralToolbar = !hideStructuralStateParam && symbol ? (
    <div className="structural-state-toolbar">
      <button
        type="button"
        className="structural-state-toggle-btn"
        onClick={toggleEventPanel}
        aria-label="切换事件状态面板"
      >
        {eventPanelCollapsed ? '显示事件状态' : '隐藏事件状态'}
      </button>
    </div>
  ) : null

  return (
    <div className="tv-content" ref={containerRef}>
      {/* ===== 股票信息栏 ===== */}
      <div className="tv-symbol-bar">
        <div className="tv-symbol-left">
          <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
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
            <b className={priceSummary.isUp ? 'market-up' : 'market-down'}>{priceSummary.currentPrice !== null ? priceSummary.currentPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>涨跌</span>
            <b className={priceSummary.isUp ? 'market-up' : 'market-down'}>
              {priceSummary.changePercent !== null ? `${priceSummary.isUp ? '+' : ''}${priceSummary.changePercent.toFixed(2)}%` : '--'}
            </b>
          </div>
          <div>
            <span>开盘</span>
            <b>{priceSummary.openPrice !== null ? priceSummary.openPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最高</span>
            <b>{priceSummary.highPrice !== null ? priceSummary.highPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最低</span>
            <b>{priceSummary.lowPrice !== null ? priceSummary.lowPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>成交额</span>
            <b>{priceSummary.amountValue !== null ? formatAmount(priceSummary.amountValue) : '--'}</b>
          </div>
        </div>
        {/* 操作：加入/移出自选、切换、全屏（截图模式隐藏全部按钮） */}
        {!isCaptureMode && (
          <div className="actions">
            <button
              className={clsx('btn', detailActions.inWatchlist ? 'danger' : 'primary')}
              onClick={detailActions.handleToggleWatchlist}
              disabled={!instrumentId || detailActions.addWatchlistPending || detailActions.removeWatchlistPending}
            >
              {detailActions.inWatchlist ? '移出自选' : '加入自选'}
            </button>
            <button className="btn small" onClick={() => detailActions.navigateToStock(-1)} disabled={!detailActions.canNavigate}>
              上一只
            </button>
            <button className="btn small" onClick={() => detailActions.navigateToStock(1)} disabled={!detailActions.canNavigate}>
              下一只
            </button>
            <button className="btn small" onClick={handleFullscreen}>
              {isFullscreen ? '退出全屏' : '全屏查看'}
            </button>
            <button className="btn small" onClick={() => detailActions.setMemoOpen(true)}>
              备忘录
            </button>
            <button
              className="btn small"
              onClick={feishu.handleOpenFeishu}
              disabled={!instrumentId}
            >
              发送到飞书
            </button>
          </div>
        )}
      </div>

      {/* 备忘录模态框 */}
      {detailActions.memoOpen && (
        <div className="modal-backdrop open" onClick={() => detailActions.setMemoOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>备忘录 - {inst.name}</h3>
              <button className="icon-btn" onClick={() => detailActions.setMemoOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              <textarea
                className="memo-textarea"
                value={detailActions.memoContent}
                onChange={(e) => detailActions.setMemoContent(e.target.value)}
                placeholder="输入备忘录内容..."
                rows={6}
              />
              <label className="memo-switch">
                <input
                  type="checkbox"
                  checked={detailActions.memoNotify}
                  onChange={(e) => detailActions.setMemoNotify(e.target.checked)}
                />
                <span>当该股票盘中触发监控事件时，在飞书通知中附带此备忘录</span>
              </label>
            </div>
            <div className="modal-foot">
              {detailActions.hasMemo && (
                <button
                  className="btn danger"
                  onClick={handleDeleteMemo}
                  disabled={detailActions.deleteMemo.isPending}
                >
                  删除
                </button>
              )}
              <button
                className="btn primary"
                onClick={handleSaveMemo}
                disabled={detailActions.upsertMemo.isPending || !detailActions.memoContent.trim()}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {/* [StockDetailFeishu] - 发送到飞书模态框：后端自动选择唯一 active 渠道 + 异步轮询状态 */}
      {feishu.feishuOpen && (
        <div
          className="modal-backdrop open"
          onClick={feishu.handleCloseFeishu}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>发送到飞书 - {inst.name}</h3>
              <button
                className="icon-btn"
                onClick={feishu.handleCloseFeishu}
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              <p className="feishu-channel-label">
                点击发送将把当前个股详情（含备忘录）推送到您已启用的飞书渠道。
              </p>

              {(feishu.feishuResult || feishu.feishuStatus || feishu.feishuPolling) && (
                <div className="feishu-status-box">
                  {feishu.feishuPolling && !feishu.feishuStatus && (
                    <div className="feishu-status-polling">投递中...</div>
                  )}
                  {feishu.feishuStatus && (
                    <>
                      <div className="feishu-status-row">
                        卡片投递:{' '}
                        <b className={`feishu-status-${feishu.feishuStatus.card_status}`}>
                          {feishu.feishuStatus.card_status}
                        </b>
                      </div>
                      {feishu.feishuStatus.image_status !== 'not_created' && (
                        <div className="feishu-status-row">
                          图片投递:{' '}
                          <b className={`feishu-status-${feishu.feishuStatus.image_status}`}>
                            {feishu.feishuStatus.image_status}
                          </b>
                        </div>
                      )}
                      {feishu.feishuStatus.overall_status === 'failed' && (
                        <div className="feishu-status-error">
                          <div>失败步骤: {feishu.feishuStatus.failed_step ?? '-'}</div>
                          <div>错误码: {feishu.feishuStatus.error_code ?? '-'}</div>
                          <div>错误信息: {feishu.feishuStatus.error_message ?? '-'}</div>
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button
                className="btn primary"
                disabled={
                  feishu.sendFeishuPending ||
                  feishu.feishuPolling ||
                  !instrumentId
                }
                onClick={feishu.handleSendFeishu}
              >
                {feishu.sendFeishuPending
                  ? '发送中...'
                  : feishu.feishuPolling
                    ? '投递中...'
                    : '发送'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 工作区：左栏来源股票列表 + 复用 StockResearchWorkspace ===== */}
      {/* [returnTo 上下文恢复] - 左栏优先展示 returnTo URL 的来源上下文：
          - returnTo 指向 /market?scope=market&query=xxx 时显示「行情搜索」列表
          - returnTo 缺失或非市场搜索时回退到「自选列表」 */}
      <div className="tv-detail-layout">
        {!isCaptureMode && detailActions.sourceStocks.length > 0 && (
          <aside className="tv-source-list" data-testid="detail-source-list">
            <div className="tv-source-list-header">
              {detailActions.sourceListKind === 'market' ? '行情来源' : '自选来源'}
            </div>
            {detailActions.sourceStocks.map((s) => (
              <div
                key={s.symbol}
                className={clsx('tv-source-list-item', s.symbol === symbol && 'active')}
                onClick={() => navigate(`/stock/${s.symbol}?source=${source}&strategy=${strategy}${returnToParam ? `&returnTo=${encodeURIComponent(returnToParam)}` : ''}`)}
              >
                <span className="tv-source-name">{s.name}</span>
                <span className="tv-source-symbol">{s.symbol}</span>
              </div>
            ))}
          </aside>
        )}
        {/* 复用 StockResearchWorkspace（图表 + 状态条） + 结构状态因子面板 */}
        <StockResearchWorkspace
          data={researchData}
          timeframe={timeframe}
          onTimeframeChange={handleTimeframeChange}
          source={source}
          strategyKey={strategy}
          isCaptureMode={isCaptureMode}
          rightPanelCollapsed={!(shouldShowPanel && !!symbol)}
          toolbar={structuralToolbar}
          rightPanel={eventStatePanel}
          showRightPanel={shouldShowPanel && !!symbol}
          chartColumnProps={{ 'data-testid': 'stock-detail-capture' }}
        />
      </div>
    </div>
  )
}
