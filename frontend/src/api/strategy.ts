// Strategy 领域 API owner
//
// [S3-C] 由 endpoints.ts 迁出。endpoints.ts 仅保留兼容 barrel 重新导出，
// 既有 caller 的 import path 与运行时行为完全不变。
//
// 边界：本文件只包含 **非 admin** 的 strategy 只读/查询契约
//   - /v1/strategies/*
//   - /v1/strategy-runs/*
//   - /v1/strategy-events/*
//   - /v1/instruments/{id}/monitor-states、/v1/instruments/{id}/events（按策略维度聚合的只读视图）
//
// 明确留在 endpoints.ts（admin domain，/v1/admin/*）：
//   createStrategy / releaseStrategyVersion / archiveStrategyVersion /
//   triggerStrategyRun / getAdminStrategyRuns（以及 TriggerRunRequest）。
//
// 依赖方向：client ← strategy ← endpoints(barrel) / useStrategyApi。

import { apiClient } from './client'

// ============================================================
// Strategy / StrategyVersion 类型
// ============================================================

/** 策略定义 */
export interface Strategy {
  id: string
  strategy_key: string
  kind: string
  display_name: string
  created_at: string
}

/** 策略列表响应 */
export interface StrategyListResponse {
  items: Strategy[]
  total: number
}

/** 策略版本 */
export interface StrategyVersion {
  id: string
  strategy_definition_id: string
  version: string
  status: string
  build_hash: string
  released_at: string | null
  manifest: Record<string, unknown>
}

/** 策略版本列表响应 */
export interface StrategyVersionListResponse {
  items: StrategyVersion[]
  total: number
}

/** 策略版本 schema 响应 */
export interface StrategySchema {
  strategy_id: string
  version: string
  kind: string
  parameters: Record<string, unknown>[]
  outputs: Record<string, unknown>[]
  input: Record<string, unknown>
  capabilities: Record<string, unknown>
}

// ============================================================
// Strategy Run 领域类型
// ============================================================

/** 策略运行记录 */
export interface StrategyRun {
  id: string
  strategy_version_id: string
  run_type: string
  trade_date: string | null
  data_cutoff: string | null
  status: string
  input_overrides: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  idempotency_key: string
  published_at: string | null
  total_instruments: number | null
  succeeded_count: number | null
  failed_count: number | null
  skipped_count: number | null
}

/** 策略运行列表响应 */
export interface StrategyRunListResponse {
  items: StrategyRun[]
  total: number
}

/** 策略运行结果 */
// [全量 universe] - 描述: id/payload 等字段可空（skipped/failed 行无 strategy_results 记录）
export interface StrategyResult {
  id: string | null
  run_id: string | null
  strategy_version_id: string | null
  instrument_id: string
  instrument_symbol?: string
  instrument_name?: string
  instrument_market?: string
  trade_date: string | null
  payload: Record<string, unknown> | null
  created_at: string | null
  // 全量 universe 改造新增字段
  item_status: string
  reason_code?: string
  error_message?: string
  // CHANGE-20260714-001: 最新行情涨跌幅（从 bars_daily 最新两根日线计算，与 DSA run 日期分离）
  // 前端"涨跌幅"列优先且只显示此字段；无两根有效日线时为 null（显示"--"）
  latest_change_pct?: number | null
  latest_change_trade_date?: string | null
}

/** 策略运行结果列表响应（分页） */
export interface StrategyResultListResponse {
  items: StrategyResult[]
  total: number
  page: number
  page_size: number
  source_total?: number
  filtered_total?: number
}

// ============================================================
// Monitor State 领域类型
// ============================================================

/** 监控状态 */
export interface MonitorState {
  strategy_version_id: string
  instrument_id: string
  bar_time: string
  calculation_id: string
  state_schema_version: number
  payload: Record<string, unknown>
  updated_at: string
}

/** 监控状态列表响应 */
export interface MonitorStateListResponse {
  items: MonitorState[]
  total: number
}

// ============================================================
// Strategy Event 领域类型
// ============================================================

/** 策略事件（列表项，不含 snapshot） */
export interface StrategyEvent {
  id: string
  event_key: string
  strategy_version_id: string
  instrument_id: string
  event_type: string
  event_time: string
  logical_entity_id: string | null
  schema_version: number
  payload: Record<string, unknown>
  created_at: string
}

/** 策略事件详情（含 snapshot 快照） */
export interface StrategyEventDetail extends StrategyEvent {
  snapshot: Record<string, unknown>
}

/** 策略事件列表响应 */
export interface StrategyEventListResponse {
  items: StrategyEvent[]
  total: number
}

/** 策略事件查询参数 */
export interface StrategyEventQueryParams {
  event_type?: string
  start_time?: string
  end_time?: string
  limit?: number
}

/** 策略运行结果查询参数 */
export interface StrategyResultQueryParams {
  matched_only?: boolean
  metric_filters?: string
  keyword?: string
  industry?: string
  concept?: string
  sort_by?: string
  sort_desc?: boolean
  page?: number
  page_size?: number
  limit?: number
  offset?: number
  universe?: 'all' | 'watchlist'
}

// ============================================================
// ===== Strategies 端点 =====
// ============================================================

/** 获取策略列表（支持 kind 过滤） */
export async function getStrategies(kind?: string): Promise<StrategyListResponse> {
  const { data } = await apiClient.get<StrategyListResponse>('/v1/strategies', { params: { kind } })
  return data
}

/** 获取策略详情 */
export async function getStrategy(strategyKey: string): Promise<Strategy> {
  const { data } = await apiClient.get<Strategy>(`/v1/strategies/${strategyKey}`)
  return data
}

/** 获取策略的所有版本 */
export async function getStrategyVersions(strategyKey: string): Promise<StrategyVersionListResponse> {
  const { data } = await apiClient.get<StrategyVersionListResponse>(`/v1/strategies/${strategyKey}/versions`)
  return data
}

/** 获取策略版本的 schema（参数/输出/输入/能力） */
export async function getStrategyVersionSchema(strategyKey: string, version: string): Promise<StrategySchema> {
  const { data } = await apiClient.get<StrategySchema>(
    `/v1/strategies/${strategyKey}/versions/${version}/schema`,
  )
  return data
}

// ============================================================
// ===== Strategy Runs 端点 =====
// ============================================================

/** 查询策略运行历史（admin） */
export async function getStrategyRuns(
  strategyKey: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/v1/strategies/${strategyKey}/runs`,
    { params },
  )
  return data
}

/** 查询已发布的运行批次（普通用户可访问，无需 admin 权限） */
export async function getPublishedRuns(
  strategyKey: string,
  params?: { limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/v1/strategies/${strategyKey}/published-runs`,
    { params },
  )
  return data
}

/** 查询运行结果（分页+筛选+排序） */
export async function getStrategyRunResults(
  runId: string,
  params?: StrategyResultQueryParams,
): Promise<StrategyResultListResponse> {
  const { data } = await apiClient.get<StrategyResultListResponse>(
    `/v1/strategy-runs/${runId}/results`,
    { params },
  )
  return data
}

/** 获取单个运行结果详情 */
export async function getStrategyRunResultDetail(
  runId: string,
  resultId: string,
): Promise<StrategyResult> {
  const { data } = await apiClient.get<StrategyResult>(
    `/v1/strategy-runs/${runId}/results/${resultId}`,
  )
  return data
}

// ============================================================
// ===== Monitor States 端点 =====
// ============================================================

/** 查询某股票的所有监控策略状态 */
export async function getInstrumentMonitorStates(instrumentId: string): Promise<MonitorStateListResponse> {
  const { data } = await apiClient.get<MonitorStateListResponse>(
    `/v1/instruments/${instrumentId}/monitor-states`,
  )
  return data
}

/** 查询某策略的所有股票状态（支持 version 过滤） */
export async function getStrategyMonitorStates(
  strategyKey: string,
  version?: string,
): Promise<MonitorStateListResponse> {
  const { data } = await apiClient.get<MonitorStateListResponse>(
    `/v1/strategies/${strategyKey}/monitor-states`,
    { params: { version } },
  )
  return data
}

// ============================================================
// ===== Strategy Events 端点 =====
// ============================================================

/** 查询某股票的策略事件 */
export async function getInstrumentEvents(
  instrumentId: string,
  params?: StrategyEventQueryParams,
  options?: { signal?: AbortSignal },
): Promise<StrategyEventListResponse> {
  const { data } = await apiClient.get<StrategyEventListResponse>(
    `/v1/instruments/${instrumentId}/events`,
    { params, signal: options?.signal },
  )
  return data
}

/** 查询某策略的事件（支持 version/event_type/时间范围过滤） */
export async function getStrategyEvents(
  strategyKey: string,
  params?: { version?: string } & StrategyEventQueryParams,
): Promise<StrategyEventListResponse> {
  const { data } = await apiClient.get<StrategyEventListResponse>(
    `/v1/strategies/${strategyKey}/events`,
    { params },
  )
  return data
}

/** 查询事件详情（含 snapshot 快照） */
export async function getStrategyEventDetail(eventId: string): Promise<StrategyEventDetail> {
  const { data } = await apiClient.get<StrategyEventDetail>(`/v1/strategy-events/${eventId}`)
  return data
}
