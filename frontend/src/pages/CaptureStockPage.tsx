// [Capture] - 描述: 专用 Capture 页面 - 截图模式专用，不经过 ProtectedLayout/AppShell
//
// 用法：路由 /capture/stock/:symbol?capture=feishu&token=xxx&instrument_id=xxx
//
// 设计要点（修复 C.7 调查发现的 30s 截图超时根因）：
// 1. 不经过 ProtectedLayout / SubscriberRoute / AppShell（避免认证守卫与全局布局副作用）
// 2. 只使用 captureClient（不使用 apiClient），capture token 由本页自行写入 CAPTURE_TOKEN_KEY
// 3. 只发起一个业务数据请求：GET /api/v1/capture/stocks/{instrument_id}/snapshot
//    后端 Snapshot 一次返回 instrument / bars / indicators / events / quote
//    不加载 watchlist / memo / events / batchInstruments（避免不必要查询阻塞渲染）
// 4. data-render-ready 只依赖 bars + indicators 加载完成（不依赖 events）
//    历史根因：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
// 5. 全屏渲染图表区域，无侧栏/导航/操作按钮/模态框
// 6. 复用 StockDetailPage 的图表组件（StrategyChart）与策略配置（resolveStrategy）
//
// [CHANGE-20260720-Phase4 §四] 移动舞台改造：
//   - 旧版 1920×1200 PC 布局 → 新版 1440×2560 9:16 移动舞台（MobileIndicatorStage）
//   - 视觉参考：ref/panji_short_video_integrated_studio_v1_15_event_flash_fix
//
// [CHANGE-20260728-010] 固定组合视图（结构 + 筹码共识）：
//   - 不再从 URL indicator_view 参数决定单指标视图；旧 URL 携带的 node_cluster/bollinger/smc
//     仅作历史兼容读取，不再影响图层渲染。
//   - 固定使用 FEISHU_CAPTURE_VIEW='structure_node'（结构 + 筹码共识组合视图）。
//   - 图层固定：node=true（profile/poc/peak_node/trigger_node）+ smc=true（BOS/CHoCH/OB/EQH/EQL）
//     + volume=true；boll=false；其余 macd/sqzmom/breakout/trend=false。
//   - combined Ready = nodeReady && smcContractReady（SMC 数组允许为空，避免永久 loading）。

import { useEffect, useMemo, useState, useCallback } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { captureClient } from '@/api/client'
import { CAPTURE_TOKEN_KEY } from '@/store/auth'
import { FEISHU_CAPTURE_VIEW } from '@/api/endpoints'
import StrategyChart from '@/components/StrategyChart'
import MobileIndicatorStage from '@/components/MobileIndicatorStage'
import type { ChartViewport } from '@/components/chartViewport'
import type { CaptureSnapshotResponse, IndicatorResponse, IndicatorView } from '@/api/endpoints'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { mapBarsToBarData } from '@/utils/chart'
import { DEFAULT_TIMEFRAME } from '@/features/stock-research/stockResearchTypes'

// [MobileIndicatorStage] 图表区域高度常量
// 几何推导（与 global.scss 中 .mobile-stage-chart-card / .mobile-stage-chart-viewport 对齐）：
//   stage-h (2560) - chart-card.top (262) - chart-card.bottom (430) - chart-head.height (112) = 1756
//   [CHANGE-20260724-002] chart-card.bottom 从 240 调整为 430，为二维码 guide-card 页脚留出空间
// 当 isCaptureMode && 在 mobile-stage 内时，StrategyChart 工具栏通过 CSS 隐藏，
// canvas-wrap 占满 chart-viewport 全高度。
const MOBILE_STAGE_CHART_HEIGHT = 1756

// [2026-07-21 反馈] 飞书移动舞台默认显示窗口：最近 90 根 bar
//   不影响底层数据拉取总长度（snapshot 仍返回 250 根日线），也不影响详情页用户缩放逻辑
//   只控制 StrategyChart 在 capture 模式下的初始 viewport
const MOBILE_STAGE_DEFAULT_VISIBLE_BARS = 90

export default function CaptureStockPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams] = useSearchParams()

  // [capture-mode] 写入 capture token 到独立 storage key
  // 本页不经过 ProtectedLayout（ProtectedLayout 负责在 /stock/:symbol 路由写入 token），
  // 需自行将 URL token 写入 CAPTURE_TOKEN_KEY，captureClient 拦截器从该 key 读取并注入 Authorization
  useEffect(() => {
    const captureToken = searchParams.get('token')
    if (captureToken) {
      localStorage.setItem(CAPTURE_TOKEN_KEY, captureToken)
    }
  }, [searchParams])

  // 解析 URL 参数：instrument_id 由 capture worker URL 传入；strategy 默认 watchlist_monitor
  const instrumentId = searchParams.get('instrument_id') || undefined
  const source = 'watchlist' as const
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR

  // 策略定义（复用 StockDetailPage 的策略解析逻辑）
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // [capture-realtime] - 截图周期优先使用 URL 传入的 timeframe（默认 1d），支持盘中 15m 等
  const timeframeParam = searchParams.get('timeframe') || DEFAULT_TIMEFRAME
  const [timeframe] = useState<string>(timeframeParam)
  const sourceBarTime = searchParams.get('source_bar_time') || undefined
  // [chartViewport] - 每个周期独立保存 viewport（截图模式仅日线，保留结构以复用 StrategyChart 受控 viewport）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})
  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe((prev) => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // [CHANGE-20260728-010] 固定组合视图：忽略 URL indicator_view 参数
  //   - 新业务唯一写入值：FEISHU_CAPTURE_VIEW='structure_node'（结构 + 筹码共识组合视图）
  //   - 旧 URL 携带的 node_cluster|bollinger|smc 仅作历史兼容读取，不再影响图层渲染
  //   - 该值固定透传给 StrategyChart 和 MobileIndicatorStage，用于 module-label 显示与 data-indicator-view 属性
  //   - 后端 snapshot API 也忽略该参数，固定按 structure_node 组合视图渲染（include_smc=true）
  const indicatorView: IndicatorView = FEISHU_CAPTURE_VIEW

  // [Task 2] focus_event 解析：从 URL query 读取监控触发事件信息
  //   字段：focus_event_id / focus_event_type / anchor_time / confirmed_time /
  //   level / bar_high / bar_low / bias / internal / bullish / eqhl_type / second_pivot_time
  //   传递到 StrategyChart.focusEventId/focusEventType，前端据此突出本次触发事件，
  //   淡化其他历史结构（半透明 / 不绘制标签）
  const focusEventId = searchParams.get('focus_event_id') || null
  const focusEventType = searchParams.get('focus_event_type') || null
  const focusEventAnchorTime = searchParams.get('anchor_time') || null
  const focusEventConfirmedTime = searchParams.get('confirmed_time') || null
  const focusEventLevel = searchParams.get('level')
  const focusEventBarHigh = searchParams.get('bar_high')
  const focusEventBarLow = searchParams.get('bar_low')
  const focusEventBias = searchParams.get('bias')
  const focusEventInternal = searchParams.get('internal')
  const focusEventBullish = searchParams.get('bullish')
  const focusEventEqhlType = searchParams.get('eqhl_type')
  const focusEventSecondPivotTime = searchParams.get('second_pivot_time')
  const focusEventInfo = useMemo(() => {
    if (!focusEventId) return null
    return {
      focus_event_id: focusEventId,
      focus_event_type: focusEventType,
      anchor_time: focusEventAnchorTime,
      confirmed_time: focusEventConfirmedTime,
      level: focusEventLevel,
      bar_high: focusEventBarHigh,
      bar_low: focusEventBarLow,
      bias: focusEventBias,
      internal: focusEventInternal,
      bullish: focusEventBullish,
      eqhl_type: focusEventEqhlType,
      second_pivot_time: focusEventSecondPivotTime,
    }
  }, [focusEventId, focusEventType, focusEventAnchorTime, focusEventConfirmedTime,
      focusEventLevel, focusEventBarHigh, focusEventBarLow,
      focusEventBias, focusEventInternal, focusEventBullish,
      focusEventEqhlType, focusEventSecondPivotTime])

  // [Capture] - 描述: 截图模式唯一业务数据请求
  // 通过 Capture Token 访问专用 Snapshot API，不调用普通业务端点
  // [CHANGE-20260728-010] indicator_view 固定传 'structure_node'，后端忽略该参数，
  //   固定按组合视图渲染（include_smc=true，Node 数据完整 + SMC DTO 结构存在）。
  const snapshotQuery = useQuery({
    queryKey: ['capture', 'snapshot', instrumentId, indicatorView],
    queryFn: async () => {
      if (!instrumentId) throw new Error('缺少 instrument_id 参数')
      const { data } = await captureClient.get<CaptureSnapshotResponse>(
        `/api/v1/capture/stocks/${instrumentId}/snapshot`,
        {
          params: {
            timeframe,
            // [CHANGE-20260728-010] 透传固定 indicator_view='structure_node' 到后端 snapshot
            //   后端忽略该参数的渲染逻辑，固定 include_smc=true；该值仅用于缓存键维度（iv=structure_node）
            //   和 CaptureJob 元数据记录，与后端 FEISHU_CAPTURE_VIEW 对齐
            indicator_view: indicatorView,
            ...(sourceBarTime ? { source_bar_time: sourceBarTime } : {}),
            // 截图链路固定强制实时计算，跳过 Redis 指标缓存，不复用旧指标
            force_refresh: 1,
            capture: 1,
          },
        },
      )
      return data
    },
    enabled: !!instrumentId,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false, // 截图为静态场景，不轮询
  })

  const snapshot = snapshotQuery.data
  const inst = snapshot?.instrument
  const barsResponse = snapshot?.bars
  const indicatorsResponse = snapshot?.indicators

  // 转换 Bar 数据为 StrategyChart 需要的 BarData 格式
  const bars = useMemo(() => mapBarsToBarData(barsResponse?.items), [barsResponse])

  // 最新报价（Snapshot 当前未单独返回 quote，使用 bars 最后一根 bar）
  const lastBar = barsResponse?.items?.[barsResponse.items.length - 1] || null
  const currentPrice = lastBar?.close ?? null

  // [2026-07-21 反馈] 当天涨跌幅：最新价相对前收（倒数第二根 close）
  //   旧口径"累计涨跌幅"用首根 close 到末根 close，不符合用户预期（应显示当日涨跌幅）
  //   日线场景：最后一根 = 当日 bar（盘中为 partial），倒数第二根 = 昨日收盘
  //   盘中 15m 等周期同理：最后一根 = 当前 bar，倒数第二根 = 前一根
  //   后端 snapshot 已按 adjustment_as_of=trade_date 截止；前端只展示，不重算
  const prevBar = barsResponse?.items?.[barsResponse.items.length - 2] || null
  const changePercent = useMemo(() => {
    if (!prevBar || !lastBar || !prevBar.close) return null
    return ((lastBar.close - prevBar.close) / prevBar.close) * 100
  }, [prevBar, lastBar])

  // 当前 K 线日期（用于 chart-head time 显示）
  // 优先 trade_time（盘中含时分），回退 trade_date（仅日期）
  // 与 mapBarsToBarData 的 time 字段构造保持一致
  const chartDate = lastBar?.trade_time || lastBar?.trade_date || null

  // [feishu-capture] - 描述: 截图模式渲染就绪标志
  // 只依赖 bars + indicators 加载完成（不依赖 events）
  // 历史根因：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
  //
  // [PROMPT.md §二 V2 render_frame.matched] Capture 必须检查服务端校验后的 frame match：
  //   - render_frame.matched=false 时不得 Ready（禁止 Capture 继续绕过合同）
  //   - mismatch 时显示两端 count/time/hash/as_of 差异，便于运维定位
  //   - 用户可点击"重试"按钮触发 snapshotQuery.refetch()
  //
  // [CHANGE-20260728-010] combined Ready = nodeReady && smcContractReady
  //   - nodeReady: data.node_cluster 含 100 行 profile + node_regions_hash + node_regions
  //   - smcContractReady: data.smc DTO 结构存在（events/order_blocks/swing_bias 等数组）
  //     SMC 数组允许为空（无事件时 SMC 结构仍需存在，避免前端永久 loading）
  //   - 旧 bollinger 分支已移除（不再作为 Ready 条件）
  //   - 旧 smc 单独 Ready 合并到 combined Ready：不再单独检查 algorithm_version
  const renderFrame = snapshot?.render_frame
  const isFrameMatched = renderFrame?.matched === true
  const hasBaseData = !!barsResponse?.items?.length && !!indicatorsResponse
  const isTypeReady = computeCombinedReady(indicatorsResponse)
  const isRenderReady = hasBaseData && isFrameMatched && isTypeReady

  // [PROMPT.md §5.3.3 V2] 发送时间：后端 snapshot_time（UTC ISO），由 MobileIndicatorStage 转 Asia/Shanghai
  const snapshotTime = snapshot?.snapshot_time ?? null

  // 加载状态：股票信息加载中
  if (snapshotQuery.isLoading) {
    return (
      <MobileIndicatorStage
        stockName="—"
        stockSymbol={symbol || ''}
        indicatorView={indicatorView}
        currentPrice={null}
        changePercent={null}
        chartDate={null}
        state="loading"
        stateMessage={
          <>
            <span className="mobile-stage-loading-spinner" />
            <b>正在获取股票数据</b>
          </>
        }
      />
    )
  }

  // 股票不存在、缺少 instrument_id 或查询出错
  if (!inst) {
    return (
      <MobileIndicatorStage
        stockName="未找到股票"
        stockSymbol={symbol || ''}
        indicatorView={indicatorView}
        currentPrice={null}
        changePercent={null}
        chartDate={null}
        state="error"
        stateMessage={
          <>
            <b>未找到股票</b>
            <span>{symbol || ''}</span>
            <small>
              {!instrumentId
                ? '缺少 instrument_id 参数'
                : snapshotQuery.isError
                  ? '股票信息查询失败，请稍后重试'
                  : '请检查股票代码是否正确'}
            </small>
          </>
        }
      />
    )
  }

  // [PROMPT.md §二 V2] render_frame.matched=false 时不得 Ready，显示 mismatch 差异
  //   禁止 Capture 继续绕过合同（旧版 isRenderReady 只检查数据存在，未检查帧匹配）
  if (renderFrame && !isFrameMatched) {
    return (
      <MobileIndicatorStage
        stockName={inst.name}
        stockSymbol={inst.symbol}
        indicatorView={indicatorView}
        currentPrice={currentPrice}
        changePercent={changePercent}
        chartDate={chartDate}
        snapshotTime={snapshotTime}
        state="mismatch"
        stateMessage={
          <>
            <b>展示帧不匹配（Capture Frame Mismatch）</b>
            <span>{inst.symbol} · {indicatorView}</span>
            <small>
              bars_count={renderFrame.bars_count ?? 'N/A'} / indicators_count={renderFrame.indicators_count ?? 'N/A'}
            </small>
            <small>
              bars_first={renderFrame.bars_first_time ?? 'N/A'} / indicators_first={renderFrame.indicators_first_time ?? 'N/A'}
            </small>
            <small>
              bars_last={renderFrame.bars_last_time ?? 'N/A'} / indicators_last={renderFrame.indicators_last_time ?? 'N/A'}
            </small>
            <small>
              bars_hash={renderFrame.bars_hash || 'N/A'}
            </small>
            <small>
              indicators_hash={renderFrame.indicators_hash || 'N/A'}
            </small>
            <small>
              bars_as_of={renderFrame.bars_adjustment_as_of ?? 'N/A'} / indicators_as_of={renderFrame.indicators_adjustment_as_of ?? 'N/A'}
            </small>
            <button
              type="button"
              onClick={() => snapshotQuery.refetch()}
              style={{ marginTop: 16, padding: '8px 24px', fontSize: 28, cursor: 'pointer' }}
            >
              重试
            </button>
          </>
        }
      />
    )
  }

  return (
    <MobileIndicatorStage
      stockName={inst.name}
      stockSymbol={inst.symbol}
      indicatorView={indicatorView}
      currentPrice={currentPrice}
      changePercent={changePercent}
      chartDate={chartDate}
      snapshotTime={snapshotTime}
      renderReady={isRenderReady}
    >
      {bars.length === 0 ? (
        <div className="mobile-stage-chart-placeholder">行情数据加载中...</div>
      ) : (
        <StrategyChart
          symbol={inst.symbol}
          displayName={inst.name}
          bars={bars}
          indicators={indicatorsResponse}
          strategyId={strategyDef.id}
          source={source}
          height={MOBILE_STAGE_CHART_HEIGHT}
          timeframe={timeframe}
          viewport={viewportByTimeframe[timeframe]}
          onViewportChange={handleViewportChange}
          isCaptureMode
          indicatorView={indicatorView}
          // [2026-07-21 反馈] 飞书移动舞台默认显示最近 90 根 bar（不改底层数据拉取，不改详情页缩放）
          defaultVisibleBars={MOBILE_STAGE_DEFAULT_VISIBLE_BARS}
          // [PROMPT.md §5.3.4 V2] Capture 强制使用 mobile_capture 缩放：
          //   1440×2560 舞台需要 ≥32px Canvas 字号 / 2.5-3.5px 线宽，桌面端保持默认 'desktop'。
          renderDensity="mobile_capture"
          // [Task 2] focus_event 透传：突出本次触发事件，淡化其他历史结构
          focusEventId={focusEventId}
          focusEventType={focusEventType}
          focusEventInfo={focusEventInfo}
        />
      )}
    </MobileIndicatorStage>
  )
}

/**
 * [CHANGE-20260728-010] combined Ready = nodeReady && smcContractReady
 *
 * - nodeReady: data.node_cluster 含 profile_rows + node_regions_hash + node_regions
 *   （兼容 watchlist_monitor / volume_node_monitor 旧命名空间回读）
 * - smcContractReady: data.smc DTO 结构存在（events/order_blocks/swing_bias 等核心数组字段）
 *   SMC 数组允许为空（无事件时 SMC 结构仍需存在，避免前端永久 loading）
 *
 * 旧 indicator_view 分支（node_cluster/bollinger/smc）已合并为组合视图检查；
 * bollinger 不再作为 Ready 条件（BB 不进入飞书截图）。
 *
 * 基础 Ready（bars 存在 + indicators 存在 + frame matched）由调用方检查，
 * 本函数只检查组合视图的额外条件。
 */
function computeCombinedReady(indicators: IndicatorResponse | undefined): boolean {
  if (!indicators?.data) return false
  const data = indicators.data as Record<string, unknown>

  // ===== Node Ready =====
  // 兼容旧命名空间：node_cluster > watchlist_monitor > volume_node_monitor
  const vn = (data['node_cluster'] ?? data['watchlist_monitor'] ?? data['volume_node_monitor']) as
    | Record<string, unknown>
    | undefined
  if (!vn) return false
  const profileRows = vn.profile_rows
  const nodeRegionsHash = vn.node_regions_hash
  const profileHash = vn.profile_hash
  const nodeRegions = vn.node_regions
  const hasHash =
    (typeof nodeRegionsHash === 'string' && nodeRegionsHash.length > 0) ||
    (typeof profileHash === 'string' && profileHash.length > 0)
  const nodeReady =
    Array.isArray(profileRows) && profileRows.length > 0 &&
    hasHash &&
    Array.isArray(nodeRegions)
  if (!nodeReady) return false

  // ===== SMC Contract Ready =====
  // DTO 结构必须存在；数组允许为空（无事件时不能导致永久 loading）
  // 实际 API 返回扁平结构：events/order_blocks/equal_highs_lows/trailing/swing_bias/pivots/params/view
  const smc = data['smc'] as Record<string, unknown> | undefined
  if (!smc) return false
  // 核心字段必须为数组类型（即使为空也代表 SMC 结构已就绪）
  const events = smc.events
  const orderBlocks = smc.order_blocks
  const swingBias = smc.swing_bias
  const params = smc.params
  const hasSmcStructure =
    Array.isArray(events) &&
    Array.isArray(orderBlocks) &&
    Array.isArray(swingBias) &&
    typeof params === 'object' && params !== null
  return hasSmcStructure
}
