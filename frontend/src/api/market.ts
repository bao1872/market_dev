// Market 领域 API owner
//
// [S3-B] 由 endpoints.ts 迁出。endpoints.ts 仅保留兼容 barrel 重新导出，
// 既有 caller 的 import path 与运行时行为完全不变。
//
// 边界：仅 /v1/market/* 只读契约（status / stocks / boards / filter-specs）。
// 注意：market Excel 导出（POST /v1/market/export）当前由 MarketWorkspacePage
// 直接内联调用，不经本模块；本轮为纯物理搬迁，不动任何 caller。
//
// 依赖方向：client ← market ← endpoints(barrel) / useMarketApi / watchlist。

import { apiClient } from './client'

// ============================================================
// ===== Market Status =====
// ============================================================

/** 市场阶段枚举（6 值，与 backend app.services.market_status_service 对齐） */
export type MarketSession =
  | 'NON_TRADING_DAY'
  | 'PRE_OPEN'
  | 'MORNING_SESSION'
  | 'LUNCH_BREAK'
  | 'AFTERNOON_SESSION'
  | 'MARKET_CLOSED'

/** 市场状态 */
export interface MarketStatus {
  is_trading_day: boolean
  is_trading_hours: boolean
  status_text: string  // "交易中" / "已收盘" / "休市" / "盘前"（向后兼容）
  market_session: MarketSession  // 6 值枚举
}

/** 查询当前 A 股市场状态（交易日/交易时段/状态文本） */
export async function getMarketStatus(): Promise<MarketStatus> {
  const { data } = await apiClient.get<MarketStatus>('/v1/market/status')
  return data
}

// ============================================================
// ===== Market Stocks 端点（PRD §8.1 行情列表）=====
// ============================================================

/** 行情列表单行（对齐后端 MarketStockRow） */
export interface MarketStockRow {
  instrument_id: string
  symbol: string
  name: string
  latest_price: number | null
  change_pct: number | null
  industry: string | null
  concepts: string[]
  dsa_state: string | null
  structure_state: string | null
  latest_event_title: string | null
  latest_event_time: string | null
  is_watchlisted: boolean
  /**
   * 第一金字塔扁平化字段（99 个 fp_ 键），来自 summary_payload.first_pyramid。
   * None 表示无最近完成交易日快照；前端显示 "—"，不即时计算。
   * 键分组：快照7 / 趋势18 / 结构8 / 结构事件21 / 动量13 / 动量事件9 / 筹码10 / 量能13。
   */
  first_pyramid: Record<string, unknown> | null
  /**
   * [CHANGE-20260729-009] DSA 策略结果 payload（含 dsa_dir_bars/vwap_ret_avg 等原 DSA 字段）。
   * 无匹配时为 null；DSA 列通过 pickPayload 读取此字段。
   */
  payload: Record<string, unknown> | null
  /** 快照所属 run ID（已发布 stock_core pointer.data_run_id） */
  data_run_id: string | null
  /** 第一金字塔必选维度是否就绪（趋势/结构/动量均有权威字段非空） */
  factor_ready: boolean | null
  /**
   * 因子错误代码：
   * - INSUFFICIENT_DAILY_BARS（日线不足，非失败）
   * - COMPUTE_FAILED（程序异常）
   * - no_snapshot / trend_missing / structure_missing / momentum_missing
   * 无错误时为 null
   */
  factor_error: string | null
  /** 实际日线数（仅 INSUFFICIENT_DAILY_BARS/COMPUTE_FAILED 时有值） */
  factor_actual_bars: number | null
  /** 最低要求日线数（=60，仅 INSUFFICIENT_DAILY_BARS/COMPUTE_FAILED 时有值） */
  factor_required_bars: number | null
  /**
   * [CHANGE-20260730-010] 筹码共识结构化状态（camelCase，与 /first-pyramid 详情 API 同口径）。
   * 无 chip 记录且无快照时为 null；有快照但 chip job 未跑时 state=pending。
   */
  chip_status: {
    state: 'ready' | 'pending' | 'failed' | 'unavailable' | 'stale'
    reasonCode: string | null
    reasonText: string | null
    computedAt: string | null
    actualBars?: number | null
    requiredBars?: number | null
    fullQualityBars?: number | null
  } | null
}

/** 行情列表分页响应（对齐后端 MarketStocksResponse） */
export interface MarketStocksResponse {
  items: MarketStockRow[]
  page: number
  page_size: number
  total: number
  price_as_of: string | null
  state_as_of: string | null
  boards_as_of: string | null
}

/** 行情列表查询参数 */
export interface MarketStocksQueryParams {
  scope: 'market' | 'watchlist'
  query?: string
  page?: number
  page_size?: number
  sort?: string
  industry?: string
  concept?: string
  state?: string
  // [CHANGE-20260729-004 P0-1] 第一金字塔字段服务端筛选/排序
  // fp_filter: key:op:val[;val2];key2:op2:val2（按 ; 分割多条件，between 用 val1;val2）
  // fp_sort: key:direction
  fp_filter?: string
  fp_sort?: string
}

/**
 * 查询行情列表（服务端分页 + 批量加载，禁止 N+1）。
 * GET /market/stocks?scope&query&page&page_size&sort&industry&concept&state&fp_filter&fp_sort
 * 每行一次返回页面所需全部字段（价格/涨跌幅/DSA状态/事件/自选）。
 * [CHANGE-20260729-004] fp_filter/fp_sort 通过 JSON 路径标量子查询在分页前完成第一金字塔字段筛选/排序。
 */
export async function getMarketStocks(
  params: MarketStocksQueryParams,
  options?: { signal?: AbortSignal },
): Promise<MarketStocksResponse> {
  const { data } = await apiClient.get<MarketStocksResponse>('/v1/market/stocks', { params, signal: options?.signal })
  return data
}

// ===== C9: 板块目录只读 API（行业/概念筛选下拉支持）=====

/** 板块目录单行 */
export interface MarketBoardItem {
  id: string
  name: string
  type: 'industry' | 'concept'
  external_code: string
}

/** 板块目录列表响应 */
export interface MarketBoardsResponse {
  items: MarketBoardItem[]
  available: boolean
  reason_code: string | null
  updated_at: string | null
  source: string | null
  stale: boolean
  last_attempt_status: string | null
}

/**
 * 查询板块目录（只读，C9）。
 * GET /market/boards?type=industry|concept
 * qstock 同步前返回空列表（不报错）。
 */
export async function getMarketBoards(
  params?: { type?: 'industry' | 'concept' },
  options?: { signal?: AbortSignal },
): Promise<MarketBoardsResponse> {
  const { data } = await apiClient.get<MarketBoardsResponse>('/v1/market/boards', { params, signal: options?.signal })
  return data
}

// ============================================================
// ===== [CHANGE-20260730-013] Market Filter Specs 端点 =====
// ============================================================

/** 第一金字塔字段 data_type 枚举（与后端 FP_QUERY_FIELD_SPECS 对齐） */
export type FpDataType = 'text' | 'enum' | 'boolean' | 'number' | 'percent' | 'datetime' | 'multi_enum'

/** 第一金字塔字段 input_control 枚举 */
export type FpInputControl =
  | 'text_input'
  | 'single_select'
  | 'multi_select'
  | 'number_input'
  | 'date_picker'
  | 'boolean_toggle'

/** 第一金字塔字段 value_normalizer 枚举 */
export type FpValueNormalizer = 'trim' | 'upper' | 'lower' | 'none'

/** 单个 fp 字段的筛选元数据（对齐后端 serialize_fp_query_field_specs 输出） */
export interface FpFieldSpec {
  fp_key: string
  data_type: FpDataType
  operators: string[]
  enum_values: string[]
  input_control: FpInputControl
  value_normalizer: FpValueNormalizer
}

/** 全部 99 字段的筛选元数据（key 为 fp_key） */
export type FpFieldSpecs = Record<string, FpFieldSpec>

/**
 * 获取第一金字塔 99 字段的筛选元数据。
 * GET /market/filter-specs
 * [CHANGE-20260730-013] 前端筛选器根据 data_type/operators/enum_values/input_control
 * 动态生成类型化控件（enum 用下拉、datetime 用日期选择器等）。
 * 普通用户即可读取（不需要 admin）。
 */
export async function getMarketFilterSpecs(
  options?: { signal?: AbortSignal },
): Promise<FpFieldSpecs> {
  const { data } = await apiClient.get<FpFieldSpecs>('/v1/market/filter-specs', { signal: options?.signal })
  return data
}
