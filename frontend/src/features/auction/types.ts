// [AuctionTypes] - 描述: 竞价分析模块 TypeScript 类型定义
// 对应后端 schemas/auction.py（字段命名与后端 JSON 序列化保持一致：snake_case）
// 规则：前端不重算业务结论，只承载结构化展示；接口必须显示 trade_date、algorithm_version、
// publication_id、source run IDs、coverage 和 reason_codes
//
// 后端用户侧 GET 接口权限为 require_authenticated（任何登录用户可读），不触发计算

// ============================================================
// 锚点（AnchorItem / AnchorSnapshot / AnchorPublication）
// ============================================================

/** 个股锚点（structure / chip / composite） */
export interface AnchorItem {
  id: string
  snapshot_id: string
  trade_date: string
  instrument_id: string
  /** [P0-FE 2026-07-31] 股票代码（如 000021），导航使用 symbol 而非 UUID */
  symbol: string | null
  /** [P0-FE 2026-07-31] 股票名称（如 深科技） */
  name: string | null
  /** 锚点类型：structure / chip / composite */
  anchor_type: string
  /** 方向：up / down */
  direction: string
  lower_price: string
  upper_price: string
  center_price: string
  strength: number
  freshness: string
  validity: string
  price_adjustment_version: string
  structure_payload: Record<string, unknown> | null
  chip_payload: Record<string, unknown> | null
  distance_at_close: string | null
  is_active: boolean
  reason_codes: string[]
}

/** 锚点快照（当日全市场锚点集合状态） */
export interface AnchorSnapshot {
  id: string
  trade_date: string
  source_core_run_id: string
  source_chip_run_id: string | null
  algorithm_version: string
  price_adjustment_version: string
  status: string
  eligible_count: number
  ready_count: number
  coverage_ratio: number
  missing_count: number
  missing_reasons: Record<string, unknown>
  structure_anchor_count: number
  chip_anchor_count: number
  composite_anchor_count: number
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

/** 锚点发布指针（最新已发布锚点） */
export interface AnchorPublication {
  id: string
  trade_date: string
  snapshot_id: string
  algorithm_version: string
  source_core_run_id: string
  source_chip_run_id: string | null
  coverage_ratio: number
  published_at: string
  superseded_by: string | null
}

/** GET /v1/auction/anchors/{trade_date} 响应 */
export interface AnchorStatusResponse {
  snapshot: AnchorSnapshot | null
  publication: AnchorPublication | null
  reason_codes: string[]
}

// ============================================================
// 竞价扫描（ScanRun / InstrumentResult / ScopeResult / EventTracking）
// ============================================================

/** 竞价扫描 run（final / opening） */
export interface ScanRun {
  id: string
  trade_date: string
  /** final（最终竞价）/ opening（开盘验证） */
  auction_type: string
  source_anchor_snapshot_id: string | null
  source_anchor_publication_id: string | null
  algorithm_version: string
  price_adjustment_version: string
  status: string
  eligible_count: number
  ready_count: number
  coverage_ratio: number
  missing_count: number
  missing_reasons: Record<string, unknown>
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

/** 个股竞价结果 */
export interface AuctionFinalQuote {
  symbol: string
  market: string
  final_price: string | null
  prev_close: string | null
  volume: number | null
  amount: string | null
  source_timestamp: string | null
  source_server: string | null
  raw_payload: Record<string, unknown>
  capture_time: string
  is_final_auction: boolean
}

export interface InstrumentResult {
  id: string
  scan_run_id: string
  trade_date: string
  instrument_id: string
  /** [P0-FE 2026-07-31] 股票代码（如 000021） */
  symbol: string | null
  /** [P0-FE 2026-07-31] 股票名称（如 深科技） */
  name: string | null
  final_quote: AuctionFinalQuote | null
  final_auction_price: string | null
  prev_close: string | null
  change_pct: number | null
  auction_volume: number | null
  auction_amount: string | null
  relative_volume_median_20d: number | null
  volume_percentile: number | null
  atr_distance_pct: number | null
  is_suspended: boolean
  is_limit_up: boolean
  is_limit_down: boolean
  is_ex_right: boolean
  /** 结构位置 */
  structure_position: string | null
  /** 筹码位置 */
  chip_position: string | null
  event_type: string | null
  event_lifecycle: string | null
  /** 参与度：high / medium / low / none */
  participation_level: string | null
  /** 趋势背景 */
  trend_background: string | null
  anchor_ids: string[] | null
  detail_payload: Record<string, unknown> | null
  reason_codes: string[]
}

/** 板块/市场竞价聚合（广度、参与度、集中度） */
export interface ScopeResult {
  id: string
  scan_run_id: string
  trade_date: string
  /** market / industry / concept */
  scope_type: string
  scope_id: string | null
  scope_name: string | null
  total_count: number
  valid_count: number
  coverage_ratio: number
  open_high_count: number
  open_flat_count: number
  open_low_count: number
  median_change_pct: number | null
  p25_change_pct: number | null
  p75_change_pct: number | null
  equal_weight_change_pct: number | null
  amount_weight_change_pct: number | null
  // 突破/破位广度
  structure_breakout_count: number
  structure_breakdown_count: number
  chip_cross_up_count: number
  chip_cross_down_count: number
  dual_breakout_count: number
  dual_breakdown_count: number
  resistance_zone_count: number
  support_zone_count: number
  // 参与度
  participation_median: number | null
  abnormal_volume_pct: number | null
  // 集中度
  top3_contribution: number | null
  top5_contribution: number | null
  hhi: number | null
  leader_median_gap: number | null
  // 分布
  positive_coverage: number | null
  negative_coverage: number | null
  dispersion: number | null
  // 后端生成的状态标签与置信度（前端只展示，不重算）
  status_label: string | null
  confidence_level: string | null
  payload: Record<string, unknown>
  reason_codes: string[]
}

/** 竞价事件追踪（个股事件） */
export interface EventTracking {
  id: string
  scan_run_id: string
  trade_date: string
  instrument_id: string
  /** [P0-FE 2026-07-31] 股票代码（如 000021） */
  symbol: string | null
  /** [P0-FE 2026-07-31] 股票名称（如 深科技） */
  name: string | null
  event_type: string
  /** formed / confirmed / continued / weakened / failed / transformed / expired */
  lifecycle: string
  anchor_id: string | null
  trigger_price: string | null
  trigger_condition: string | null
  formed_at: string | null
  confirmed_at: string | null
  weakened_at: string | null
  failed_at: string | null
  expired_at: string | null
  confirmation_data: Record<string, unknown> | null
  reason_codes: string[]
}

// ============================================================
// 页面数据（市场/板块/个股三级页面）
// ============================================================

/** GET /v1/auction 响应 — 市场级页面数据 */
export interface AuctionMarketPageData {
  trade_date: string
  algorithm_version: string
  publication_id: string | null
  scan_run_id: string | null
  source_core_run_id: string | null
  source_chip_run_id: string | null
  coverage: number | null
  reason_codes: string[]
  market_scope: ScopeResult | null
  industry_scopes: ScopeResult[]
  concept_scopes: ScopeResult[]
  top_events: EventTracking[]
}

/** GET /v1/auction/board/{board_id} 响应 — 板块级页面数据 */
export interface AuctionBoardPageData {
  trade_date: string
  algorithm_version: string
  scan_run_id: string | null
  scope: ScopeResult | null
  top_instruments: InstrumentResult[]
  events: EventTracking[]
  reason_codes: string[]
}

/** GET /v1/auction/stock/{symbol} 响应 — 个股级页面数据 */
export interface AuctionInstrumentPageData {
  trade_date: string
  algorithm_version: string
  scan_run_id: string | null
  instrument_id: string | null
  anchors: AnchorItem[]
  result: InstrumentResult | null
  events: EventTracking[]
  reason_codes: string[]
}

// ============================================================
// 前端展示用枚举常量（仅用于 UI 标签映射，禁止用作业务计算）
// ============================================================

/** 锚点类型标签 */
export const ANCHOR_TYPE_LABELS: Record<string, string> = {
  structure: '结构',
  chip: '筹码',
  composite: '复合',
}

/** 锚点方向标签 */
export const ANCHOR_DIRECTION_LABELS: Record<string, string> = {
  up: '向上',
  down: '向下',
}

/** 扫描类型标签 */
export const AUCTION_TYPE_LABELS: Record<string, string> = {
  final: '最终竞价',
  opening: '开盘验证',
}

/** 事件生命周期标签 */
export const EVENT_LIFECYCLE_LABELS: Record<string, string> = {
  formed: '形成',
  confirmed: '确认',
  continued: '延续',
  weakened: '减弱',
  failed: '失效',
  transformed: '转化',
  expired: '过期',
}

/** 参与度标签 */
export const PARTICIPATION_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
  none: '无',
}

// ============================================================
// 竞价事件回流（ReviewPage 第二金字塔面板）
// ============================================================

/** 锚点新鲜度分布桶 */
export interface AuctionAnchorFreshnessBucket {
  freshness: string
  anchor_count: number
  active_count: number
}

/** 事件迁移行：lifecycle 转换计数 */
export interface AuctionEventMigrationRow {
  from_lifecycle: string | null
  to_lifecycle: string
  event_count: number
  sample_instrument_ids: string[]
}

/** 集中度信息（市场或行业 scope） */
export interface AuctionConcentrationInfo {
  hhi?: number | null
  top3_contribution?: number | null
  top5_contribution?: number | null
  leader_median_gap?: number | null
  dispersion?: number | null
  scope_id?: string | null
  scope_name?: string | null
  median_change_pct?: number | null
}

/** GET /v1/auction/backflow/{trade_date} 响应 — ReviewPage 第二金字塔数据 */
export interface AuctionBackflowData {
  trade_date: string
  algorithm_version: string
  scan_run_id: string | null
  anchor_publication_id: string | null
  source_core_run_id: string | null
  source_chip_run_id: string | null
  /** 分布：event_type → count */
  event_type_distribution: Record<string, number>
  /** 分布：lifecycle → count */
  lifecycle_distribution: Record<string, number>
  /** 迁移：lifecycle 转换计数 */
  event_migrations: AuctionEventMigrationRow[]
  /** 新鲜度：锚点按 freshness 桶分布 */
  anchor_freshness_buckets: AuctionAnchorFreshnessBucket[]
  /** 集中度：market scope */
  market_concentration: AuctionConcentrationInfo
  /** 集中度：top3 行业 */
  top_industry_concentration: AuctionConcentrationInfo[]
  /** 事件回流（与 review 信号匹配的事件） */
  backflow_events: EventTracking[]
  reason_codes: string[]
}
