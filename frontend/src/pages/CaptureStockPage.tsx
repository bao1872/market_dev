// [Capture] - 描述: 专用 Capture 页面 - 截图模式专用，不经过 ProtectedLayout/AppShell
//
// 用法：路由 /capture/stock/:symbol?capture=feishu&token=xxx&instrument_id=xxx&indicator_view=smc
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
//   - URL 新增 indicator_view=node_cluster|bollinger|smc 参数
//     · 携带时使用 INDICATOR_VIEW_LAYER_PRESETS（每张图只渲染一个指标视图）
//     · 缺失时回退到 FEISHU_CAPTURE_LAYERS（向后兼容旧 capture URL）
//   - indicatorView 通过 props 传递给 StrategyChart（替代旧版 isCaptureMode 强制 5 层）

import { useEffect, useMemo, useState, useCallback } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { captureClient } from '@/api/client'
import { CAPTURE_TOKEN_KEY } from '@/store/auth'
import StrategyChart from '@/components/StrategyChart'
import MobileIndicatorStage from '@/components/MobileIndicatorStage'
import type { ChartViewport } from '@/components/chartViewport'
import type { CaptureSnapshotResponse, IndicatorResponse, IndicatorView } from '@/api/endpoints'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { mapBarsToBarData } from '@/utils/chart'
import {
  normalizeIndicatorView,
  DEFAULT_TIMEFRAME,
} from '@/features/stock-research/stockResearchTypes'

// 默认 indicator_view（与后端 DEFAULT_INDICATOR_VIEW 对齐）
// 当 URL 未携带 indicator_view 参数时使用，保证新版本截图链路始终产出"单一指标视图"
const DEFAULT_CAPTURE_INDICATOR_VIEW: IndicatorView = 'node_cluster'

// [MobileIndicatorStage] 图表区域高度常量
// 几何推导（与 global.scss 中 .mobile-stage-chart-card / .mobile-stage-chart-viewport 对齐）：
//   stage-h (2560) - chart-card.top (262) - chart-card.bottom (240) - chart-head.height (112) = 1946
// 当 isCaptureMode && 在 mobile-stage 内时，StrategyChart 工具栏通过 CSS 隐藏，
// canvas-wrap 占满 chart-viewport 全高度。
const MOBILE_STAGE_CHART_HEIGHT = 1946

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

  // [CHANGE-20260720-Phase4 §四] 解析 indicator_view URL 参数
  //   - 合法值：node_cluster | bollinger | smc（与后端 INDICATOR_VIEW_VALUES 对齐）
  //   - 非法或缺失：回退到 DEFAULT_CAPTURE_INDICATOR_VIEW（node_cluster）
  //   - 透传到 StrategyChart prop，影响图层预设；同时作为 MobileIndicatorStage 的 module-label 文案
  const indicatorView: IndicatorView = useMemo(() => {
    const raw = searchParams.get('indicator_view')
    const normalized = normalizeIndicatorView(raw)
    return normalized ?? DEFAULT_CAPTURE_INDICATOR_VIEW
  }, [searchParams])

  // [Capture] - 描述: 截图模式唯一业务数据请求
  // 通过 Capture Token 访问专用 Snapshot API，不调用普通业务端点
  const snapshotQuery = useQuery({
    queryKey: ['capture', 'snapshot', instrumentId, indicatorView],
    queryFn: async () => {
      if (!instrumentId) throw new Error('缺少 instrument_id 参数')
      const { data } = await captureClient.get<CaptureSnapshotResponse>(
        `/api/v1/capture/stocks/${instrumentId}/snapshot`,
        {
          params: {
            timeframe,
            // [CHANGE-20260720-Phase4] 透传 indicator_view 到后端 snapshot
            //   后端可基于此参数决定 include_smc 等计算开关（smc 视图需要 include_smc=true）
            //   也可用于缓存键维度（iv=smc）与 CaptureJob 元数据记录
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

  // [MobileIndicatorStage] 累计涨跌幅：从可见 bar 首根 close 到末根 close
  //   注意：这是简化口径（仅基于 snapshot 返回的 bars 计算），与产品约定的"区间累计涨跌幅"对齐
  //   后端 snapshot 已按 adjustment_as_of=trade_date 截止；前端只展示，不重算
  const firstBar = barsResponse?.items?.[0] || null
  const changePercent = useMemo(() => {
    if (!firstBar || !lastBar || !firstBar.close) return null
    return ((lastBar.close - firstBar.close) / firstBar.close) * 100
  }, [firstBar, lastBar])

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
  // [PROMPT.md §5.3.5 V2 类型特定 Ready] 不同 indicator_view 有额外 Ready 条件：
  //   - node_cluster: 100 行 profile + profile_hash + node_regions
  //   - bollinger: 三轨（upper/middle/lower）+ frame matched
  //   - smc: DTO 存在 + 版本正确 + frame matched
  const renderFrame = snapshot?.render_frame
  const isFrameMatched = renderFrame?.matched === true
  const hasBaseData = !!barsResponse?.items?.length && !!indicatorsResponse
  const isTypeReady = computeTypeSpecificReady(indicatorView, indicatorsResponse)
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
          // [PROMPT.md §5.3.4 V2] Capture 强制使用 mobile_capture 缩放：
          //   1440×2560 舞台需要 ≥32px Canvas 字号 / 2.5-3.5px 线宽，桌面端保持默认 'desktop'。
          renderDensity="mobile_capture"
        />
      )}
    </MobileIndicatorStage>
  )
}

/**
 * [PROMPT.md §5.3.5 V2 类型特定 Ready] 不同 indicator_view 的额外 Ready 条件。
 *
 * - node_cluster: data.node_cluster 含 100 行 profile + profile_hash + node_regions
 * - bollinger: data.bb_monitor 含三轨（upper/middle/lower 非空数组）
 * - smc: data.smc 含 DTO + algorithm_version
 *
 * 基础 Ready（bars 存在 + indicators 存在 + frame matched）由调用方检查，
 * 本函数只检查类型特定条件。
 */
function computeTypeSpecificReady(
  indicatorView: IndicatorView,
  indicators: IndicatorResponse | undefined,
): boolean {
  if (!indicators?.data) return false
  const data = indicators.data as Record<string, unknown>

  if (indicatorView === 'node_cluster') {
    // [PROMPT.md §5.3.5] Node: 100 行 profile + profile_hash + node_regions
    const vn = (data['node_cluster'] ?? data['watchlist_monitor'] ?? data['volume_node_monitor']) as
      | Record<string, unknown>
      | undefined
    if (!vn) return false
    const profileRows = vn.profile_rows
    const profileHash = vn.profile_hash
    const nodeRegions = vn.node_regions
    return (
      Array.isArray(profileRows) && profileRows.length > 0 &&
      typeof profileHash === 'string' && profileHash.length > 0 &&
      Array.isArray(nodeRegions)
    )
  }

  if (indicatorView === 'bollinger') {
    // [PROMPT.md §5.3.5] BB: 三轨（upper/middle/lower）非空
    const bb = (data['bb_monitor'] ?? data['bollinger']) as Record<string, unknown> | undefined
    if (!bb) return false
    const upper = bb.upper
    const middle = bb.middle
    const lower = bb.lower
    return (
      Array.isArray(upper) && upper.length > 0 &&
      Array.isArray(middle) && middle.length > 0 &&
      Array.isArray(lower) && lower.length > 0
    )
  }

  if (indicatorView === 'smc') {
    // [PROMPT.md §5.3.5] SMC: DTO 存在 + 版本正确
    const smc = data['smc'] as Record<string, unknown> | undefined
    if (!smc) return false
    const algorithmVersion = smc.algorithm_version
    const bos = smc.bos
    const choch = smc.choch
    return (
      typeof algorithmVersion === 'string' && algorithmVersion.length > 0 &&
      (Array.isArray(bos) || Array.isArray(choch))
    )
  }

  return true
}
