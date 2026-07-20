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

import { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import { useParams, useSearchParams, useNavigate, useLocation } from 'react-router-dom'
import clsx from 'clsx'
import { AtomicFactsDrawer } from '@/features/research-context/AtomicFactsDrawer'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { StockQuoteStrip } from '@/features/stock-research/StockQuoteStrip'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { useStockDetailActions } from '@/features/stock-research/useStockDetailActions'
import { useStockDetailFeishu } from '@/features/stock-research/useStockDetailFeishu'
import {
  type DisplayTimeframe,
  normalizeDisplayTimeframe,
} from '@/features/stock-research/stockResearchTypes'
// [CHANGE-011 SMC] - 加载初始 layerVisibility.smc 状态，驱动 indicators 按需重拉
import { loadChartLayerVisibility } from '@/features/stock-research/indicatorPreferences'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { MARKET_LABELS } from '@/utils/market'
import { resolveBackPath } from './detailNavigation'
import { useToast } from '@/store/toast'
import { changePctColorClass, fmtChange } from '@/features/trend-selection'
import { resolveDetailSourceContext } from '@/features/stock-research/detailSourceContext'
import { buildStockDetailUrl } from '@/features/stock-research/stockDetailNavigation'

// CHANGE-20260714-001: 左栏来源列表滚动位置 sessionStorage key 前缀
// key 由 returnTo + scope 生成稳定 hash，避免不同来源上下文串扰
const SOURCE_LIST_SCROLL_KEY_PREFIX = 'panji:detail-source-scroll:v1'

function makeSourceListScrollKey(returnTo: string | null, scope: string | null): string {
  // 简单 hash：returnTo + scope 字符串拼接（无需密码学强度，仅做 namespace 隔离）
  const raw = `${returnTo ?? ''}|${scope ?? ''}`
  return `${SOURCE_LIST_SCROLL_KEY_PREFIX}:${raw}`
}

export default function StockDetailPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const showToast = useToast((s) => s.show)

  // CHANGE-20260716-006: originScope 为来源唯一真源，resolveDetailSourceContext 优先使用
  // 优先级：显式 originScope > 有效 /market returnTo.scope（兼容旧链接）> 默认 watchlist
  const returnToParam = searchParams.get('returnTo')
  const originScopeParam = searchParams.get('originScope') as 'market' | 'watchlist' | null
  const { source, strategy, marketContext, sourceContextInvalid } = resolveDetailSourceContext(
    returnToParam,
    searchParams.get('source'),
    searchParams.get('strategy'),
    originScopeParam,
  )
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

  // [CHANGE-011 SMC] - smc 开关状态：初始值从 localStorage 读取（与 StockResearchWorkspace 同源），
  //   用户在 IndicatorToolbar 切换 smc 时由 onSmcToggle 回调更新；驱动 useStockResearchData 重拉 indicators。
  //   默认关闭；后端 include_smc=False 时跳过 SMC 计算，不消耗 CPU。
  const [smcEnabled, setSmcEnabled] = useState<boolean>(() =>
    loadChartLayerVisibility(source, strategy).smc,
  )
  // source/strategy 变化时重新读取 smc 偏好（与 StockResearchWorkspace useEffect 同步）
  useEffect(() => {
    setSmcEnabled(loadChartLayerVisibility(source, strategy).smc)
  }, [source, strategy])
  const handleSmcToggle = useCallback((enabled: boolean) => {
    setSmcEnabled(enabled)
  }, [])

  // 共享研究数据 hook（/market 和 /stock 共用，只含核心查询）
  const researchData = useStockResearchData({ symbol: symbol ?? null, timeframe, includeSmc: smcEnabled })
  const instrumentId = researchData.instrumentId

  // 详情页专属 actions（自选/上下切换/memo + returnTo 上下文恢复左栏列表）
  // CHANGE-20260715-007: 传入 resolveDetailSourceContext 的解析结果，不再各自推导
  const detailActions = useStockDetailActions({
    instrumentId,
    symbol,
    source,
    strategy,
    marketContext,
    sourceContextInvalid,
    returnTo: returnToParam,
    timeframe,
  })

  // CHANGE-20260714-001: 左栏来源列表滚动位置保存/恢复
  // 切换股票前保存 scrollTop 到 sessionStorage；新股票渲染后恢复
  // 只有活动行完全离开可视区时才 scrollIntoView({block:'nearest'})，避免每次切换都滚回顶部
  const sourceListRef = useRef<HTMLDivElement | null>(null)
  const sourceListScrollKey = useMemo(
    () => makeSourceListScrollKey(returnToParam, detailActions.sourceListKind),
    [returnToParam, detailActions.sourceListKind],
  )
  const lastSavedScrollRef = useRef<number>(0)

  // 切换股票前保存当前 scrollTop（在 navigate 之前由点击/上一只/下一只触发）
  // 由于 navigate 后组件会重新渲染，这里在 symbol 变化的 effect 中保存"上一次"的 scrollTop
  useEffect(() => {
    const el = sourceListRef.current
    if (!el) return
    // 保存当前 scrollTop（用于下次恢复）
    const saveScroll = () => {
      const cur = el.scrollTop
      lastSavedScrollRef.current = cur
      try {
        sessionStorage.setItem(sourceListScrollKey, String(cur))
      } catch {
        // sessionStorage 不可用时静默降级（隐私模式/配额满）
      }
    }
    // 在卸载或 symbol 变化前保存
    return () => saveScroll()
  }, [sourceListScrollKey, symbol])

  // 新股票渲染后恢复 scrollTop（仅在活动行不可见时 scrollIntoView）
  useEffect(() => {
    const el = sourceListRef.current
    if (!el) return
    // 先尝试恢复保存的 scrollTop
    let savedScroll: number | null = null
    try {
      const raw = sessionStorage.getItem(sourceListScrollKey)
      if (raw !== null) {
        const n = Number(raw)
        if (Number.isFinite(n)) savedScroll = n
      }
    } catch {
      // sessionStorage 不可用
    }
    if (savedScroll !== null) {
      el.scrollTop = savedScroll
    }
    // 检查活动行是否在可视区，不可见则最小幅度滚动到可见
    if (symbol) {
      const activeEl = el.querySelector<HTMLDivElement>('.tv-source-list-item.active')
      if (activeEl) {
        const containerRect = el.getBoundingClientRect()
        const itemRect = activeEl.getBoundingClientRect()
        const isVisible =
          itemRect.top >= containerRect.top &&
          itemRect.bottom <= containerRect.bottom
        if (!isVisible) {
          activeEl.scrollIntoView({ block: 'nearest' })
        }
      }
    }
  }, [sourceListScrollKey, symbol, detailActions.sourceStocks])

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

  // 右栏状态观察面板（Atomic Fact Contract V1: 点击「显示状态观察」打开右侧 overlay Drawer，
  // 不压缩主 K 线；与 /market 共用 useStockContext query key）
  const eventStatePanel = shouldShowPanel && symbol ? (
    <AtomicFactsDrawer symbol={symbol} open onClose={toggleEventPanel} />
  ) : null

  // 状态观察面板开关 toolbar（渲染在图表上方）
  const structuralToolbar = !hideStructuralStateParam && symbol ? (
    <div className="structural-state-toolbar">
      <button
        type="button"
        className="structural-state-toggle-btn"
        onClick={toggleEventPanel}
        aria-label="切换状态观察面板"
      >
        {eventPanelCollapsed ? '显示状态观察' : '隐藏状态观察'}
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
        {/* 报价条：现价/涨跌/开盘/最高/最低/成交额/总市值/流通市值（CHANGE-20260713-010） */}
        <StockQuoteStrip priceSummary={priceSummary} />
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

              {/* [CHANGE-20260720-003 §四] 指标视图三单选项：一张图/一段文案只描述一个指标 */}
              <div className="feishu-indicator-view-group" role="radiogroup" aria-label="指标视图">
                <label
                  className={`feishu-indicator-view-option${
                    feishu.selectedIndicatorView === 'node_cluster' ? ' active' : ''
                  }`}
                >
                  <input
                    type="radio"
                    name="feishu-indicator-view"
                    value="node_cluster"
                    checked={feishu.selectedIndicatorView === 'node_cluster'}
                    onChange={() => feishu.setSelectedIndicatorView('node_cluster')}
                    disabled={feishu.sendFeishuPending || feishu.feishuPolling}
                  />
                  <span className="feishu-indicator-view-label">筹码共识价</span>
                </label>
                <label
                  className={`feishu-indicator-view-option${
                    feishu.selectedIndicatorView === 'bollinger' ? ' active' : ''
                  }`}
                >
                  <input
                    type="radio"
                    name="feishu-indicator-view"
                    value="bollinger"
                    checked={feishu.selectedIndicatorView === 'bollinger'}
                    onChange={() => feishu.setSelectedIndicatorView('bollinger')}
                    disabled={feishu.sendFeishuPending || feishu.feishuPolling}
                  />
                  <span className="feishu-indicator-view-label">布林带</span>
                </label>
                <label
                  className={`feishu-indicator-view-option${
                    feishu.selectedIndicatorView === 'smc' ? ' active' : ''
                  }`}
                >
                  <input
                    type="radio"
                    name="feishu-indicator-view"
                    value="smc"
                    checked={feishu.selectedIndicatorView === 'smc'}
                    onChange={() => feishu.setSelectedIndicatorView('smc')}
                    disabled={feishu.sendFeishuPending || feishu.feishuPolling}
                  />
                  <span className="feishu-indicator-view-label">SMC 结构</span>
                </label>
              </div>

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
      {/* CHANGE-20260715-004: 来源列表加载中显示 loading 占位，避免空白后突然出现列表 */}
      {/* CHANGE-20260715-005: 尊重显式 source；拆分 loading/error/empty/invalid 状态 */}
      <div className="tv-detail-layout">
        {!isCaptureMode && detailActions.sourceListLoading && (
          <aside
            className="tv-source-list tv-source-list-loading"
            data-testid="detail-source-list-loading"
          >
            <div className="tv-source-list-header">
              {detailActions.sourceListKind === 'market' ? '行情来源' : '自选来源'}
            </div>
            <div className="tv-source-list-placeholder">加载中…</div>
          </aside>
        )}
        {!isCaptureMode && !detailActions.sourceListLoading && detailActions.sourceListError && (
          <aside
            className="tv-source-list tv-source-list-error"
            data-testid="detail-source-list-error"
          >
            <div className="tv-source-list-header">
              {detailActions.sourceListKind === 'market' ? '行情来源' : '自选来源'}
            </div>
            <div className="tv-source-list-placeholder">来源数据加载失败</div>
          </aside>
        )}
        {!isCaptureMode && !detailActions.sourceListLoading && !detailActions.sourceListError && detailActions.sourceContextInvalid && (
          <aside
            className="tv-source-list tv-source-list-invalid"
            data-testid="detail-source-list-invalid"
          >
            <div className="tv-source-list-header">行情来源</div>
            <div className="tv-source-list-placeholder">来源上下文失效</div>
          </aside>
        )}
        {!isCaptureMode && !detailActions.sourceListLoading && !detailActions.sourceListError && !detailActions.sourceContextInvalid && detailActions.sourceListEmpty && (
          <aside
            className="tv-source-list tv-source-list-empty"
            data-testid="detail-source-list-empty"
          >
            <div className="tv-source-list-header">
              {detailActions.sourceListKind === 'market' ? '行情来源' : '自选来源'}
            </div>
            <div className="tv-source-list-placeholder">
              {detailActions.sourceListKind === 'market' ? '暂无选股结果' : '暂无自选股票'}
            </div>
          </aside>
        )}
        {!isCaptureMode && !detailActions.sourceListLoading && !detailActions.sourceListError && !detailActions.sourceContextInvalid && !detailActions.sourceListEmpty && detailActions.sourceStocks.length > 0 && (
          <aside
            className="tv-source-list"
            data-testid="detail-source-list"
            ref={sourceListRef}
          >
            <div className="tv-source-list-header">
              {detailActions.sourceListKind === 'market' ? '行情来源' : '自选来源'}
            </div>
            {detailActions.sourceStocks.map((s) => (
              <div
                key={s.symbol}
                className={clsx('tv-source-list-item', s.symbol === symbol && 'active')}
                onClick={() => navigate(buildStockDetailUrl(s.symbol, { originScope: source === 'selection' ? 'market' : 'watchlist', returnTo: returnToParam, timeframe }))}
              >
                <span className="tv-source-name">{s.name}</span>
                <div className="tv-source-meta">
                  <span className="tv-source-symbol">{s.symbol}</span>
                  {/* CHANGE-20260714-001: 右侧显示最近交易日涨跌幅（两位小数，A股红涨绿跌） */}
                  {s.changePct !== null && (
                    <span className={clsx('tv-source-change-pct', changePctColorClass(s.changePct))}>
                      {fmtChange(s.changePct)}
                    </span>
                  )}
                </div>
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
          rightPanel={null}
          showRightPanel={false}
          chartColumnProps={{ 'data-testid': 'stock-detail-capture' }}
          onSmcToggle={handleSmcToggle}
        />
      </div>
      {/* 状态观察 Drawer（overlay，不压缩 K 线；开闭由 eventPanelCollapsed 控制） */}
      {eventStatePanel}
    </div>
  )
}
