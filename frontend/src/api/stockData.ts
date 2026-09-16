// Stock Data 领域 API owner（[S3-E] 由 endpoints.ts 迁出；endpoints.ts 仅兼容 barrel）。
// 统一承载个股/行情/研究数据读取链：instrument / bars / quote / indicators / chart-snapshot /
// calendar / structural·temporal factors / stock memo / stock context / first pyramid。

import { apiClient } from './client'
import type { StrategyEventListResponse } from './strategy'

export interface Instrument {
  id: string
  symbol: string
  name: string
  market: string
  status: string
  listing_date: string | null
  created_at: string
  updated_at: string
}

/** 股票列表分页响应 */
export interface InstrumentListResponse {
  items: Instrument[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ============================================================
// Strategy 领域类型
// ============================================================

// [S3-C] Strategy / StrategyVersion / StrategyRun / StrategyResult / MonitorState /
// StrategyEvent 等类型已迁至 ./strategy（见顶部兼容 re-export）。

// ============================================================
// Notification 领域类型
// ============================================================

/** 消息投递状态机：与后端 MessageDelivery.status 对齐（models/notification.py） */

export interface CaptureSnapshotResponse {
  instrument: Instrument
  bars: BarListResponse
  indicators: IndicatorResponse
  events: StrategyEventListResponse
  snapshot_time: string
  // 注意：last_live_bar_time / is_partial / data_source 只在 bars (BarListResponse) 内，
  // 前端实时状态必须从 snapshot.bars.xxx 读取，禁止从 snapshot 顶层读取
  capture: {
    user_id: string
    event_id: string
    scope: string
  }
  // [CHANGE-20260720-Phase4 §四] indicator_view 与 include_smc 透传
  indicator_view?: string
  include_smc?: boolean
  // [PROMPT.md §二 V2 render_frame.matched] 服务端校验后的 frame match 状态
  //   false 时前端 CaptureStockPage 不得 Ready（data-render-ready="false"），禁止 Capture 继续绕过合同
  render_frame?: CaptureRenderFrame
}

// [StockDetailFeishu] - 描述: 异步 Outbox 投递模式类型契约（POST 创建 + GET 状态轮询）
// 与后端 backend/app/api/stock_detail_feishu.py 的 SendFeishuResponse / ShareStatusResponse 对齐

/** 单条投递状态（card / image / capture 共用） */

export type IndicatorView = 'node_cluster' | 'bollinger' | 'smc' | 'structure_node'

// 新业务固定使用的视图常量
export const FEISHU_CAPTURE_VIEW = 'structure_node' as const

/** POST /instruments/{instrument_id}/send-feishu 响应 - 创建异步投递任务 */

export interface Bar {
  instrument_id: string
  trade_date: string | null
  trade_time: string | null
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
  adj_factor: number
}

/** 行情列表响应（服务端分页 + 数据源诊断） */
export interface BarListResponse {
  items: Bar[]
  total: number
  page: number
  page_size: number
  timeframe: string
  adj: string
  // [bars] - 数据源诊断字段
  data_source: string
  as_of: string | null
  is_partial: boolean
  // [bars] - 实时 bar 时间（对齐后端 BarListResponse schema）
  last_persisted_bar_time?: string | null
  last_live_bar_time?: string | null
  freshness_seconds: number
  degraded: boolean
  degraded_reason: string | null
  // [CHANGE-20260717-002 SSOT] - MDAS v2 契约诊断字段
  //   用于 ChartRenderFrame 帧匹配（bars 与 indicators source_bar_hash 比对）
  //   详见 PROMPT.md §五.296-305（周期切换原子渲染门禁）
  source_bar_hash?: string | null
  adj_factor_hash?: string | null
  market_data_contract_version?: string | null
  adjustment_as_of?: string | null
  // [display_frame] - 展示帧（PROMPT.md §二.1）：与 indicators API 共用 build_display_frame 生成。
  //   前端 ChartRenderFrame 优先比对 display_frame.display_hash，
  //   display_frame 缺失时降级到 source_bar_hash（向后兼容）。
  //   source_bar_hash 描述算法输入（含 warmup），display_hash 只描述真正展示给前端的 K线窗口。
  display_frame?: DisplayFrame | null
}

/**
 * 展示帧结构（bars API 与 indicators API 共用）。
 *
 * 字段对齐后端 app.services.indicator_display_frame.build_display_frame：
 *   - instrument_id / timeframe / adj：基础标识
 *   - display_times：展示窗口 bar 时间数组
 *   - display_hash：展示窗口 OHLCV SHA256 前 16 字符
 *   - completed_through：已完成到的时间
 *
 * [PROMPT.md §二 V2] 新增 V2 字段（build_display_frame 传入 DisplayWindowSpec 时附加）：
 *   - requested_count：请求的展示窗口大小（前端 BARS_COUNT_BY_TIMEFRAME）
 *   - actual_count：实际返回的 bar 数量（可能小于 requested_count）
 *   - first_time / last_time：展示窗口首末 bar 时间（ISO 字符串）
 *   - include_realtime：是否包含实时 partial bar
 *   - is_partial：最后一根 bar 是否为未完成 partial bar
 *   - adjustment_as_of：复权锚点（None=最新）
 *
 * 空数据路径：display_times=[]，display_hash=""（视为缺失，触发降级）。
 * V1 向后兼容：requested_count 等新字段为可选，旧缓存返回的 display_frame 不含这些字段。
 */
export interface DisplayFrame {
  instrument_id: string
  timeframe: string
  adj: string
  display_times: string[]
  display_hash: string
  completed_through: string | null
  // [PROMPT.md §二 V2] 新增字段（可选，V1 缓存可能缺失）
  requested_count?: number | null
  actual_count?: number | null
  first_time?: string | null
  last_time?: string | null
  include_realtime?: boolean | null
  is_partial?: boolean | null
  adjustment_as_of?: string | null
}

/**
 * Capture render_frame 比对结果（PROMPT.md §二 V2）。
 *
 * 后端 capture.py 使用 is_display_frame_match 比对 bars_display_frame 与
 * indicators display_frame 后返回此结构。前端 CaptureStockPage 必须检查
 * matched 字段，false 时不得 Ready（data-render-ready="false"）。
 *
 * 字段对齐后端 app.api.capture.py return render_frame 字典：
 *   - matched：bars 与 indicators display_frame 是否匹配
 *   - bars_hash / indicators_hash：两端 display_hash
 *   - bars_count / indicators_count：两端 actual_count
 *   - bars_first_time / indicators_first_time：两端 first_time
 *   - bars_last_time / indicators_last_time：两端 last_time
 *   - bars_adjustment_as_of / indicators_adjustment_as_of：两端 adjustment_as_of
 */
export interface CaptureRenderFrame {
  matched: boolean
  bars_hash: string
  indicators_hash: string
  bars_count: number | null
  indicators_count: number | null
  bars_first_time: string | null
  indicators_first_time: string | null
  bars_last_time: string | null
  indicators_last_time: string | null
  bars_adjustment_as_of: string | null
  indicators_adjustment_as_of: string | null
}

// ============================================================
// Calendar 领域类型
// ============================================================

/** 交易日历条目 */
export interface CalendarDay {
  id: string
  trade_date: string
  is_trading_day: boolean
  market: string
  created_at: string
}

/** 交易日历列表响应 */
export interface CalendarListResponse {
  items: CalendarDay[]
  total: number
}

/** 是否交易日查询响应 */
export interface TradingDayResponse {
  trade_date: string
  is_trading_day: boolean
  source: string
}

// ============================================================
// Admin Membership 领域类型
// ============================================================

// [plan_contract] - 描述: 套餐定义由后端 plans 表唯一真源驱动，前端不硬编码套餐字段
// 权威值为 backend/app/models/plan.py / app/services/plan_service.py，通过 GET /plans 公开端点获取
/** 套餐代码（与后端 plans.plan_code 一致） */

export interface InstrumentQueryParams {
  keyword?: string
  market?: string
  status?: string
  page?: number
  page_size?: number
}

// [S3-C] StrategyEventQueryParams / StrategyResultQueryParams 已迁至 ./strategy（见顶部兼容 re-export）。

/** 行情查询参数 */
export interface BarQueryParams {
  timeframe?: string
  adj?: string
  start_date?: string
  end_date?: string
  page?: number
  page_size?: number
}

/** 日历查询参数 */
export interface CalendarQueryParams {
  start_date?: string
  end_date?: string
  market?: string
}


/** 分页查询参数 */

export interface EventsSummaryResponse {
  date: string
  total_events: number
  instruments_with_events: number
  last_event_at: string | null
}

/** 查询当前用户指定日期的策略事件汇总 */
export async function getEventsSummary(date: string): Promise<EventsSummaryResponse> {
  const { data } = await apiClient.get<EventsSummaryResponse>('/v1/me/events/summary', {
    params: { date },
  })
  return data
}

// ============================================================
// ===== Instruments 端点 =====
// ============================================================

/** 查询股票列表，支持关键词搜索、市场/状态筛选与分页 */
export async function getInstruments(params?: InstrumentQueryParams): Promise<InstrumentListResponse> {
  const { data } = await apiClient.get<InstrumentListResponse>('/v1/instruments', { params })
  return data
}

/** 批量查询股票响应 */
export interface InstrumentBatchResponse {
  items: Instrument[]
  total: number
}

/** 按 ID 列表批量查询股票（最多 1000 个） */
export async function batchGetInstruments(ids: string[]): Promise<InstrumentBatchResponse> {
  const { data } = await apiClient.post<InstrumentBatchResponse>('/v1/instruments/batch', { ids })
  return data
}

/** 按 ID 查询单个股票 */
export async function getInstrumentById(instrumentId: string): Promise<Instrument> {
  const { data } = await apiClient.get<Instrument>(`/v1/instruments/${instrumentId}`)
  return data
}

/** 按 symbol 查询股票（symbol 唯一，最多返回 1 条） */
export async function getInstrumentBySymbol(symbol: string): Promise<Instrument> {
  const { data } = await apiClient.get<Instrument>(`/v1/instruments/by-symbol/${symbol}`)
  return data
}

// ============================================================
// ===== Strategies 端点 =====
// ============================================================
//
// [S3-C] 非 admin strategy 端点（getStrategies/getStrategy/getStrategyVersions/
// getStrategyVersionSchema/getStrategyRuns/getPublishedRuns/getStrategyRunResults/
// getStrategyRunResultDetail/getInstrumentMonitorStates/getStrategyMonitorStates/
// getInstrumentEvents/getStrategyEvents/getStrategyEventDetail）已迁至 ./strategy；
// 本文件仅保留 admin strategy 端点（createStrategy/releaseStrategyVersion/
// archiveStrategyVersion/triggerStrategyRun/getAdminStrategyRuns）。

/** 创建策略（admin）- 提交 Manifest 创建策略定义 + 草稿版本 */

export interface StockMemo {
  id: string
  user_id: string
  instrument_id: string
  content: string
  notify_feishu: boolean
  created_at: string
  updated_at: string
}

/** 备忘录 upsert 请求 */
export interface StockMemoUpsertRequest {
  content: string
  notify_feishu?: boolean
}

/** 切换飞书推送开关请求 */
export interface StockMemoNotifyToggleRequest {
  notify_feishu: boolean
}

/** 获取当前用户对指定股票的备忘录 */
export async function getStockMemo(instrumentId: string): Promise<StockMemo | null> {
  try {
    const { data } = await apiClient.get<StockMemo>(`/v1/instruments/${instrumentId}/memo`)
    return data
  } catch (err: unknown) {
    if (err && typeof err === 'object' && 'response' in err) {
      const axiosErr = err as { response?: { status?: number } }
      if (axiosErr.response?.status === 404) return null
    }
    throw err
  }
}

/** 创建/更新备忘录（upsert） */
export async function upsertStockMemo(
  instrumentId: string,
  payload: StockMemoUpsertRequest,
): Promise<StockMemo> {
  const { data } = await apiClient.put<StockMemo>(`/v1/instruments/${instrumentId}/memo`, payload)
  return data
}

/** 删除备忘录 */
export async function deleteStockMemo(instrumentId: string): Promise<void> {
  await apiClient.delete(`/v1/instruments/${instrumentId}/memo`)
}

/** 切换飞书推送开关 */
export async function toggleMemoNotify(
  instrumentId: string,
  payload: StockMemoNotifyToggleRequest,
): Promise<StockMemo> {
  const { data } = await apiClient.patch<StockMemo>(
    `/v1/instruments/${instrumentId}/memo/notify`,
    payload,
  )
  return data
}

// ============================================================
// ===== Bars 端点 =====
// ============================================================

/**
 * 查询指定标的的行情数据
 * 后端 bars router 自带 prefix="/v1"，完整路径为 /v1/instruments/{id}/bars
 * apiClient baseURL="/api" 会添加网关前缀，代理层处理后到达后端 /v1/instruments/{id}/bars
 */
export async function getBars(instrumentId: string, params?: BarQueryParams, options?: { signal?: AbortSignal }): Promise<BarListResponse> {
  const { data } = await apiClient.get<BarListResponse>(
    `/v1/instruments/${instrumentId}/bars`,
    { params, signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Quote 端点 =====
// ============================================================

/** 实时报价响应（可信来源与新鲜度） */
export interface QuoteResponse {
  instrument_id: string
  symbol: string
  name: string
  current_price: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  prev_close: number
  change_pct: number
  update_time: string
  source: 'pytdx' | 'daily_fallback'
  is_realtime: boolean
  freshness_seconds: number
  degraded: boolean
  degraded_reason: string | null
  amount?: number
  // CHANGE-20260713-010: 总市值/流通市值（数据源不可用时为 null）
  total_market_cap?: number | null
  float_market_cap?: number | null
  market_cap_as_of?: string | null
  market_cap_source?: string | null
  market_cap_degraded_reason?: string | null
}

/** 查询指定标的的实时报价（交易时段 pytdx 实时，非交易时段降级到数据库最新日线） */
export async function getQuote(instrumentId: string, options?: { signal?: AbortSignal }): Promise<QuoteResponse> {
  const { data } = await apiClient.get<QuoteResponse>(
    `/v1/instruments/${instrumentId}/quote`,
    { signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Indicators 端点 =====
// ============================================================

/** 策略图表图层定义（来自 manifest 的 chart_layers） */
export interface ChartLayer {
  strategy_id: string
  strategy_name: string
  layer_id: string
  layer_name: string
  renderer: string  // line | dsa_polyline | price_zone | marker | band
  pane: string      // price | volume | separate
  color?: string
  direction_colored?: boolean
  direction_up_color?: string
  direction_down_color?: string
  // [DSA 分段] - regime_field 指定 regime_id 字段名，前端按 regime 分段渲染（切换点不连接）
  regime_field?: string
  // [DSA 分段] - anchor_field 指定 anchor_time 字段名，前端在锚点 bar 绘制小圆点
  anchor_field?: string
  fields: string[]
  hover_fields: string[]
}

/** [DSA 分段] - 视觉段：方向 + 点序列，dsa_polyline 渲染器按段独立 beginPath/stroke */
export interface VisualSegment {
  direction: 1 | -1
  points: { time: string; value: number }[]
}

// [DSA 数据契约] - dsa_selector 策略的 data 结构（visual_segments 属于 data，不属于 ChartLayer）
//   与后端 manifest v1.4.1 对齐：visual_segments 由后端预计算，前端从 data.dsa_selector.visual_segments 读取
export interface DsaSelectorData {
  time: string[]
  visual_segments: VisualSegment[]
  dsa_vwap: (number | null)[]
  dsa_dir: number[]
  regime_id: number[]
  anchor_time: (string | null)[]
  pivot_type: (string | null)[]
  pivot_price: (number | null)[]
}

/** 指标查询参数 */
export interface IndicatorQueryParams {
  timeframe?: string  // 1d | 15m | 1h | 1w | 1mo
  adj?: string        // qfq | none
  bars?: number       // 返回最近 N 根 bar 的指标
  force_refresh?: number  // 1 时跳过 Redis 指标缓存强制实时计算（截图链路使用）
  // [CHANGE-011 SMC] - 1 时计算 SMC 指标（默认关闭；前端通过 IndicatorToolbar 显式开启）
  // 后端在 include_smc=False 时跳过 SMC 计算，不消耗 CPU
  include_smc?: number
}

/** 指标 API 响应 */
export interface IndicatorResponse {
  layers: ChartLayer[]
  // [DSA 数据契约] - data 值支持 string（anchor_time 为 ISO 字符串|null 数组，其余字段为 number|null）
  //   dsa_selector 键的值为 DsaSelectorData（含 visual_segments），其他策略保持泛型数组结构
  data: Record<string, DsaSelectorData | Record<string, (number | string | null)[]>>
  errors?: Record<string, string>
  // [CHANGE-20260719-003 §四] 后端 echo 的 timeframe 字段，供前端周期切换乱序丢弃检查
  //   前端比对 response.timeframe vs 当前 timeframe，不匹配则丢弃旧响应（PROMPT.md §4）
  timeframe?: string
  // [DSA 数据源校验] - source_bar_times 指标计算所基于的 K 线时间序列，前端与当前 K 线时间比对，不一致则跳过 DSA 渲染
  source_bar_times?: string[]
  // [DSA 数据源校验] - source_bar_hash K 线时间序列哈希，便于调试与后端联调定位数据源漂移
  source_bar_hash?: string
  // [display_frame] - 展示帧（PROMPT.md §二.1）：与 bars API 共用 build_display_frame 生成。
  //   ChartRenderFrame 优先比对 display_frame.display_hash，不再直接比对 source_bar_hash。
  //   原因：bars API 返回展示窗口 100 根，indicators API 算法输入 250 根（含 Node warmup），
  //   source_bar_hash 永远不等，导致 1d 周期永久 mismatch、指标图层被屏蔽、页面持续显示"指标加载中"。
  display_frame?: DisplayFrame | null
  // [calculation_diagnostics] - 算法输入诊断（PROMPT.md §二.1）：仅用于调试与日志，
  //   不参与前端 ChartRenderFrame 帧比对。包含 source_bar_hash/warmup_bars/calculation_window/
  //   smc_source_bar_hash/node_daily_hash/node_15m_hash/node_profile_hash 等算法输入层 hash。
  calculation_diagnostics?: CalculationDiagnostics | null
}

/**
 * 算法输入诊断（indicators API 返回，不参与前端帧比对）。
 *
 * 字段对齐后端 app.services.indicator_display_frame.build_calculation_diagnostics：
 *   - source_bar_hash / source_bar_times：算法输入 K线 hash 与时间序列
 *   - warmup_bars / calculation_window：算法 warmup 与计算窗口大小
 *   - smc_source_bar_hash：SMC 算法输入 hash（如启用）
 *   - node_daily_hash / node_15m_hash / node_profile_hash：Node Cluster 算法各输入 hash
 *   - algorithm_version / contract_fingerprint：Node Cluster 算法版本与契约指纹
 *   - market_data_contract_version / adj_factor_hash：MDAS 契约版本与复权因子 hash
 *
 * 所有字段均为可选（build_calculation_diagnostics 过滤 None 字段）。
 */
export interface CalculationDiagnostics {
  source_bar_hash?: string | null
  source_bar_times?: string[]
  warmup_bars?: number
  calculation_window?: number
  smc_source_bar_hash?: string | null
  smc_source_bar_times?: string[]
  node_daily_hash?: string | null
  node_15m_hash?: string | null
  node_profile_hash?: string | null
  algorithm_version?: string | null
  contract_fingerprint?: string | null
  market_data_contract_version?: string | null
  adj_factor_hash?: string | null
}

/**
 * 查询指定标的的所有策略图表指标
 * 后端 indicators router 自带 prefix="/v1"，完整路径为 /v1/instruments/{id}/indicators
 * apiClient baseURL="/api" 会添加网关前缀，代理层处理后到达后端 /v1/instruments/{id}/indicators
 */
export async function getIndicators(
  instrumentId: string,
  params?: IndicatorQueryParams,
  options?: { signal?: AbortSignal },
): Promise<IndicatorResponse> {
  const { data } = await apiClient.get<IndicatorResponse>(
    `/v1/instruments/${instrumentId}/indicators`,
    { params, signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Chart Snapshot 端点（PRD V2.0 §4.2 SNAP-01 Atomic Chart Snapshot）=====
// ============================================================

/**
 * Atomic Chart Snapshot 查询参数。
 *
 * [PRD V2.0 §4.2 SNAP-01] 详情页必须使用 chart-snapshot 端点，禁止独立调用 /bars + /indicators。
 * 一次请求返回 bars + indicators + display_frame + render_frame，保证同一 MDAS DataFrame。
 *
 * 参数与 /bars + /indicators 同款（DisplayWindowSpec V2）：
 * - timeframe / adj / bars：展示窗口规格
 * - include_smc：是否计算 SMC 指标（默认 0；前端通过 IndicatorToolbar 显式开启）
 * - include_realtime / completed_only / adjustment_as_of：MDAS v2 契约参数
 */
export interface ChartSnapshotQueryParams {
  timeframe?: string
  adj?: string
  bars?: number
  include_smc?: number
  include_realtime?: boolean
  completed_only?: boolean
  adjustment_as_of?: string
}

/**
 * Atomic Chart Snapshot 响应。
 *
 * 一次返回详情页图表所需的完整数据，替代独立 useBars + useIndicators 两次请求。
 * bars 和 indicators 基于同一 MDAS DataFrame 生成 display_frame，display_hash 必然一致。
 *
 * 字段对齐后端 app.api.chart_snapshot.get_chart_snapshot 返回结构：
 * - bars: BarListResponse（items + 分页 + 诊断 + display_frame）
 * - indicators: IndicatorResponse（layers + data + display_frame + calculation_diagnostics）
 * - snapshot_time: ISO 8601 时间戳
 * - render_frame: {matched, bars_hash, indicators_hash, ...} display_frame 匹配结果
 * - timeframe: 周期 echo（供前端周期切换乱序丢弃检查）
 */
export interface ChartSnapshotResponse {
  bars: BarListResponse
  indicators: IndicatorResponse
  snapshot_time: string
  render_frame: CaptureRenderFrame
  timeframe: string
  // [P0-7] 详情页唯一行情真源扩展字段（CHANGE-20260724-003）
  // 详情页不得同时以 /quote 和 /chart-snapshot 作为行情真源；
  // 顶部价格、K线和指标必须来自同一快照、同一 as_of
  quote: {
    current_price: number
    open: number
    high: number
    low: number
    close: number
    volume: number
    prev_close: number | null
    change_pct: number
    update_time: string
    is_realtime: boolean
  } | null
  market_session: string
  as_of: string
  actual_latest_bar_time: string | null
  expected_latest_bar_time: string
  freshness_state: 'fresh' | 'partial' | 'stale' | 'unavailable'
  data_source: string
  is_partial: boolean
  degraded_reason: string | null
}

/**
 * 查询指定标的的原子图表快照（PRD V2.0 §4.2 SNAP-01）。
 *
 * 一次 MDAS DataFrame 同时生成 bars + indicators + display_frame + render_frame。
 * 详情页必须使用本端点，禁止独立调用 /bars + /indicators。
 *
 * 后端 chart_snapshot router 自带 prefix="/v1"，
 * 完整路径为 /v1/instruments/{id}/chart-snapshot
 */
export async function getChartSnapshot(
  instrumentId: string,
  params?: ChartSnapshotQueryParams,
  options?: { signal?: AbortSignal },
): Promise<ChartSnapshotResponse> {
  const { data } = await apiClient.get<ChartSnapshotResponse>(
    `/v1/instruments/${instrumentId}/chart-snapshot`,
    { params, signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Calendar 端点 =====
// ============================================================

/** 查询交易日历（支持日期范围与市场筛选） */
export async function getCalendar(params?: CalendarQueryParams): Promise<CalendarListResponse> {
  const { data } = await apiClient.get<CalendarListResponse>('/v1/calendar', { params })
  return data
}

/** 查询指定日期是否为交易日（三级降级：DB -> Mootdx -> weekday） */
export async function isTradingDay(targetDate: string): Promise<TradingDayResponse> {
  const { data } = await apiClient.get<TradingDayResponse>(`/v1/calendar/is-trading-day/${targetDate}`)
  return data
}

// ============================================================
// ===== Market 端点（status / stocks / boards / filter-specs）=====
// ============================================================
//
// [S3-B] getMarketStatus / getMarketStocks / getMarketBoards / getMarketFilterSpecs 及其类型
// 的实现已迁至 ./market（唯一 owner），并在本文件顶部以兼容 barrel 重新导出。

// [S3-B] Market Stocks / Boards 的类型与端点已迁至 ./market（见顶部兼容 re-export）。


// [S3-B] Market Filter Specs 类型与端点已迁至 ./market（见顶部兼容 re-export）。

// ============================================================
// ===== Admin Membership 端点 =====
// ============================================================

/** 获取所有 active 套餐定义（公开端点，无需登录） */

export interface StructuralFactorQueryParams {
  primary_timeframe?: string
  secondary_timeframe?: string
  adj?: string
  as_of?: string
}

/**
 * 结构状态因子响应
 * 包含双周期 (1d + 15m) 5 组结构因子 + relation + meta
 *
 * V1.8：relation 移除 momentum_alignment，新增客观关系字段
 * 前端只渲染后端 DTO，严禁重新计算。
 */
export interface StructuralFactorResponse {
  primary: Record<string, Record<string, unknown> | null>
  secondary: Record<string, Record<string, unknown> | null>
  relation: {
    primary_dir?: number | null
    secondary_dir?: number | null
    trend_alignment?: string | null
    primary_swing_position?: number | null
    secondary_swing_position?: number | null
    primary_slope_atr?: number | null
    secondary_slope_atr?: number | null
    secondary_vs_primary_position_delta?: number | null
    notes?: string[]
  }
  meta: {
    as_of: string
    primary_lookback_bars: number
    secondary_lookback_bars: number
    degraded_reasons: string[]
    warmup_notes: string[]
  }
}

/**
 * 查询指定标的的双周期结构状态因子
 * 后端 structural_factors router prefix="/v1/instruments"
 * 完整路径: /v1/instruments/{id}/structural-factors
 */
export async function getStructuralFactors(
  instrumentId: string,
  params?: StructuralFactorQueryParams,
): Promise<StructuralFactorResponse> {
  const { data } = await apiClient.get<StructuralFactorResponse>(
    `/v1/instruments/${instrumentId}/structural-factors`,
    { params },
  )
  return data
}

// ============================================================
// Temporal Features V1（时序特征：daily_context + m15_response + derived_relation）
// ============================================================

/**
 * Temporal Features 查询参数。
 * V1 仅支持 as_of=latest（默认），历史回测 as_of 待 V2。
 */
export interface TemporalFeaturesQueryParams {
  as_of?: 'latest'
  primary_timeframe?: string
  secondary_timeframe?: string
  adj?: 'qfq' | 'none'
}

/**
 * 时序特征响应 DTO。
 * 与后端 temporal_feature_service.compute_temporal_features 返回结构严格对齐。
 * 前端只渲染 DTO，严禁重新计算。
 */
export interface TemporalFeaturesResponse {
  daily_context: {
    daily_dsa_dir: number | null
    daily_dsa_segment_duration_percentile: number | null
    daily_dsa_slope_atr_per_bar: number | null
    daily_dsa_efficiency_0_1: number | null
    daily_price_position_in_swing_0_1: number | null
    daily_distance_to_swing_high_atr: number | null
    daily_distance_to_node_above_atr: number | null
    daily_sqzmom_change_since_segment_start: number | null
    daily_volume_percentile_change_since_segment_start: number | null
  }
  m15_response: {
    m15_price_position_in_swing_0_1: number | null
    m15_position_change_since_swing_anchor: number | null
    m15_distance_to_swing_high_atr: number | null
    m15_distance_to_swing_low_atr: number | null
    m15_sqzmom_change_since_swing_anchor: number | null
    m15_sqzmom_abs_percentile: number | null
    m15_sqz_off: boolean | null
    m15_bb_bandwidth_change_since_swing_anchor: number | null
    m15_volume_percentile_change_since_swing_anchor: number | null
  }
  derived_relation: {
    m15_position_relative_to_daily: number | null
    m15_response_direction_relative_to_daily: string | null
    m15_response_intensity: number | null
  }
  meta: {
    as_of: string
    primary_timeframe: string
    secondary_timeframe: string
    degraded_reasons: string[]
    warmup_notes: string[]
  }
}

/**
 * 查询指定标的的时序特征 V1。
 * 后端 temporal_features router prefix="/v1/instruments"
 * 完整路径: /v1/instruments/{id}/temporal-features
 * V1 仅支持 as_of=latest，as_of=其他值后端返回 400。
 */
export async function getTemporalFeatures(
  instrumentId: string,
  params?: TemporalFeaturesQueryParams,
): Promise<TemporalFeaturesResponse> {
  const { data } = await apiClient.get<TemporalFeaturesResponse>(
    `/v1/instruments/${instrumentId}/temporal-features`,
    { params },
  )
  return data
}

// ============================================================

export interface AtomicFactItem {
  /** 稳定公开键（不随内部 factId 变动） */
  publicKey: string
  /** 维度：trend / momentum / structure / volume */
  dimension: 'trend' | 'momentum' | 'structure' | 'volume'
  /** 通俗中文短标签 */
  label: string
  /** 前端渲染类型（禁止解析中文推断类型/状态） */
  visualKind:
    | 'metric'
    | 'value_with_category'
    | 'relation'
    | 'position'
    | 'distance'
    | 'ratio'
    | 'confirmed_position'
  /** 原始数值（分类类事实为 null） */
  value: number | null
  /** 用户可读短原子值（无内部术语）；关系类事实为 null，仅以 categoryLabel 承载 */
  valueText: string | null
  /** 机器分类码（UI 可选） */
  categoryCode: string | null
  /** 中文分类标签 */
  categoryLabel: string | null
  /** 弱说明（单位/补充） */
  secondaryText: string | null
  /** 单位（如 ATR） */
  unit: string | null
  /** 分类阈值是否已启用（T5/V3 为 false → 展示「分类未启用」） */
  thresholdEnabled: boolean
}

/** AtomicFactAvailability - 可用性统计（Core 分母固定 14；缺失项从用户数组省略） */
export interface AtomicFactAvailability {
  coreDenominator: number
  corePresent: number
  /** 缺失事实 publicKey 列表（事实本身已从 core 数组省略） */
  coreMissing: string[]
  auxiliaryAvailable: string[]
  /** 默认隐藏（不在用户 UI 展示）的 Auxiliary publicKey */
  auxiliaryHidden: string[]
  v1Present: boolean
  rejectedPresent: boolean
  /** 数据质量异常（如 m5_inconsistent） */
  warnings: string[]
}

/** AtomicFactChange - 近期变化（相邻已发布快照间只读计算，按展示精度比较） */
export interface AtomicFactChange {
  publicKey: string
  /** 通俗中文短标签（前端展示，禁止显示 publicKey） */
  label: string
  dimension: string
  fromText: string | null
  toText: string | null
  /** 变化类型：分类调整 / 数值变动 / 状态更新（不解释利好利空） */
  deltaText: string
  asOf: string
}

/**
 * ProductObservationItem - 产品观察扩展项（CHANGE-20260716-006）。
 *
 * 不在冻结 Core 14 中，不参与 14/14 统计。基于底层已计算的结构因子生成，
 * 用于补充展示（如最近确认区间位置）。scope 恒为 "product"。
 */
export interface ProductObservationItem {
  /** 产品观察公开键（如 confirmed_swing_position） */
  publicKey: string
  /** 通俗中文短标签 */
  label: string
  /** 产品观察渲染类型（独立于 Core visualKind） */
  visualKind: 'confirmed_position'
  /** 所属组（如 structure） */
  group: string
  /** 区间内为 0–1 值，区间外为 null */
  value: number | null
  /** 原始值（可能 <0 或 >1，不静默 clip） */
  rawValue: number | null
  /** 用户可读短值（区间内） */
  valueText: string | null
  /** 中文分类标签（含区间外说明） */
  categoryLabel: string | null
  /** 已确认区间上沿（UI 参考） */
  confirmedHigh: number | null
  /** 已确认区间下沿（UI 参考） */
  confirmedLow: number | null
  /** 标记为产品观察，非 V4.13 Core */
  scope: 'product'
}

/**
 * ProductObservations - 产品观察扩展集合（CHANGE-20260716-006）。
 *
 * 按 group 分组（structure 等），不计入 Core 14/14 统计。
 */
export interface ProductObservations {
  /** 结构组产品观察项 */
  structure: ProductObservationItem[]
}

/** StockContext 数据质量 - 含 reasonCode 解释空态原因（被原子事实响应复用） */
export interface StockContextDataQuality {
  hasSucceededRun: boolean
  hasSnapshot: boolean
  reasonCode: string | null
  degradedReasons: string[]
  runTradeDate: string | null
  runPublishedAt: string | null
  instrumentStatus: string
}

/** AtomicFactsMeta - 公共响应 meta：三版本字段（前端禁止硬编码 V4.13） */
export interface AtomicFactsMeta {
  /** 持久化 payload schema 版本（当前 1） */
  payloadVersion: string
  /** 研究合同冻结版本（V4.13） */
  researchFreezeVersion: string
  /** 产品展示合同版本 */
  presentationVersion: string
}

/** AtomicFactsContextResponse - GET /stocks/{symbol}/context 用户侧响应（只读） */
export interface AtomicFactsContextResponse {
  contractVersion: string
  /** 三版本元数据（前端禁止硬编码 V4.13，必须从 meta 读取） */
  meta: AtomicFactsMeta
  asOf: string | null
  core: Record<string, AtomicFactItem[]>
  auxiliary: AtomicFactItem[]
  availability: AtomicFactAvailability
  /** 近期变化（仅最近一个交易日发生变化的项） */
  recentChanges: AtomicFactChange[]
  /** 近期变化起始交易日（前一发布交易日；无对比时为 null） */
  latestChangesFrom: string | null
  /** 近期变化截止交易日（最新发布交易日；无快照时为 null） */
  latestChangesAsOf: string | null
  /** 产品观察扩展（CHANGE-20260716-006，不计入 Core 14/14） */
  productObservations: ProductObservations
  dataQuality: StockContextDataQuality
}

// [Phase 5B-2 + Gate1] 第一金字塔统一快照类型（与后端 FirstPyramidSnapshot DTO 对齐）
export interface VolumeContextSchema {
  volume: number | null
  amount: number | null
  turnoverRate: number | null
  volumeMa20: number | null
  volumeMa200: number | null
  volumeRatio20: number | null
  volumeRatio200: number | null
  volumePercentile20: number | null
  volumePercentile200: number | null
  volumeZscore20: number | null
  volumeZscore200: number | null
  readiness: boolean
  badge: string | null
}

export interface PyramidEvent {
  type: string
  /** [QM-63] 正式方向值：bullish/bearish/null（up/down 仅历史兼容） */
  direction: string | null
  /** [QM-63] 正式结构级别：swing/internal/null（缺级别保持 null，不默认 swing） */
  structureLevel?: string | null
  /** [QM-63] 正式 bias：1/-1/null（由 direction 派生，二者永远一致） */
  bias?: number | null
  occurredAt: string | null
  barIndex: number | null
  price: number | null
  freshnessBars: number
  volumeContext?: VolumeContextSchema | null
  volumeBadge?: string | null
  extra?: Record<string, unknown>
}

export interface DimensionResult {
  name: string
  available: boolean
  continuousFactors: Record<string, unknown>
  events: PyramidEvent[]
  statusText: string
  evidence: Record<string, unknown>
  volumeContext?: VolumeContextSchema | null
}

export interface FirstPyramidSnapshot {
  symbol: string
  tradeDate: string
  orderedDimensions: string[]
  trend: DimensionResult
  structure: DimensionResult
  momentum: DimensionResult
  chipConsensus: DimensionResult | null
  // [CHANGE-20260729-004 P0-2] 筹码共识结构化状态（替代统一"暂不可用"文案）
  chipStatus?: ChipStatus | null
  statusText: string
  volumeContext?: VolumeContextSchema | null
  inputHash: string
  parameterHash: string
  algorithmVersion: string
  /**
   * [QM-63] run 级唯一计算时间（编排器注入）。
   * 同一 run 的所有股票共享完全相同的值；单股各自计算时为 null。
   */
  calculatedAt?: string | null
  /**
   * [QM-63] run 级唯一来源 id（编排器注入）。
   * null 表示本快照非批量 run 产出（单股即时计算），不得理解为「丢失」。
   */
  sourceRunId?: string | null
}

/**
 * [QM-63 2026-08-04] chipStatus.state 完整七态（与后端
 * `app/schemas/first_pyramid.py::CHIP_STATUS_STATES` 严格一致）。
 *
 * - pending      : chip job 已入队/运行中，尚未产出
 * - ready        : chip 结果完整可用
 * - unavailable  : 上游数据不足，本交易日合法不可算（非错误）
 * - failed       : chip 计算异常（错误，需排查）
 * - interrupted  : chip job 被取消/Worker 接管而未完成
 * - stale        : chip 结果存在但落后于 core run（旧残留）
 * - partial      : chip 部分维度可用，coverage < 1
 */
export type ChipStatusState =
  | 'pending'
  | 'ready'
  | 'unavailable'
  | 'failed'
  | 'interrupted'
  | 'stale'
  | 'partial'

/** [CHANGE-20260729-004 P0-2 + CHANGE-20260730-010 + QM-63] 筹码共识结构化状态 */
export interface ChipStatus {
  state: ChipStatusState
  reasonCode: string | null
  reasonText: string | null
  computedAt: string | null
  /** [CHANGE-20260730-010] 诊断字段：M15_BARS_INSUFFICIENT 时填充 */
  actualBars?: number | null
  requiredBars?: number | null
  fullQualityBars?: number | null
  /** [QM-63] 产出该 chip 结果的 run id（用于与 core run 比对） */
  sourceRunId?: string | null
  /** [QM-63] chip 异步任务 id（定位失败/中断的具体 job） */
  jobId?: string | null
  /** [QM-63] chip 结果落后 core 的交易日数；0 表示同日 */
  freshness?: number | null
  /** [QM-63] chip 维度覆盖度 0~1；partial 状态必须 <1 */
  coverage?: number | null
}


/**
 * 获取个股原子事实上下文（只读，需登录 + 有效订阅）。
 * GET /v1/stocks/{symbol}/context?as_of=YYYY-MM-DD
 * 返回 Atomic Fact Contract V1 上下文（contractVersion/asOf/core/auxiliary/availability/recentChanges/dataQuality）。
 */
export async function getStockContext(
  symbol: string,
  params?: { as_of?: string },
  options?: { signal?: AbortSignal },
): Promise<AtomicFactsContextResponse> {
  const { data } = await apiClient.get<AtomicFactsContextResponse>(
    `/v1/stocks/${symbol}/context`,
    { params, signal: options?.signal },
  )
  return data
}

/**
 * [Phase 5B-2] 第一金字塔统一快照（趋势→结构→动量→筹码共识）。
 * GET /v1/stocks/{symbol}/first-pyramid?as_of=YYYY-MM-DD
 * 返回 FirstPyramidSnapshot（固定维度顺序，前三维必选，chip_consensus 可选）。
 * 前端展示顺序：trend → structure → momentum → chip_consensus。
 */
export async function getFirstPyramid(
  symbol: string,
  params?: { as_of?: string },
  options?: { signal?: AbortSignal },
): Promise<FirstPyramidSnapshot> {
  const { data } = await apiClient.get<FirstPyramidSnapshot>(
    `/v1/stocks/${symbol}/first-pyramid`,
    { params, signal: options?.signal },
  )
  return data
}


