// [ReviewUrlState] - 描述: /review URL 状态解析/编码纯函数（PRD §3.1、§15）
// URL 是页面状态的唯一可分享入口（SSOT）：
//   /review?date=&stage=&scopeType=&scopeKey=&signalId=&boardId=&symbol=&trackingTab=
// 规则：
// - 首次加载先解析 URL，再写入组件状态，禁止 hydration 后被默认值覆盖
// - 切换阶段、信号、板块、股票时更新 URL
// - 浏览器前进/后退必须正确恢复状态
// - URL 不携带算法内部阈值或大段 JSON
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行
import type { ReviewStage, TrackingTab } from './types'

export interface ReviewUrlState {
  /** 交易日（YYYY-MM-DD） */
  date: string | null
  /** 五阶段：scan/signals/attribution/validation/tracking */
  stage: ReviewStage
  /** 范围类型（market/major_index/style/industry_l1/...） */
  scopeType: string | null
  /** 范围标识 */
  scopeKey: string | null
  /** 当前信号 ID */
  signalId: string | null
  /** 板块 ID（跳转 /boards/analysis 用） */
  boardId: string | null
  /** 个股代码（阶段四选中股票） */
  symbol: string | null
  /** 追踪复核子 Tab：history/watchlist/events */
  trackingTab: TrackingTab
}

/** 默认阶段：市场扫描 */
export const DEFAULT_REVIEW_STAGE: ReviewStage = 'scan'
export const DEFAULT_TRACKING_TAB: TrackingTab = 'history'

const STAGE_VALUES: ReadonlySet<string> = new Set([
  'scan',
  'signals',
  'attribution',
  'validation',
  'tracking',
])

const TRACKING_TAB_VALUES: ReadonlySet<string> = new Set([
  'history',
  'watchlist',
  'events',
])

/** 归一化阶段值，非法值回退到默认 scan */
export function normalizeStage(raw: string | null | undefined): ReviewStage {
  return raw && STAGE_VALUES.has(raw) ? (raw as ReviewStage) : DEFAULT_REVIEW_STAGE
}

/** 归一化追踪 Tab 值 */
export function normalizeTrackingTab(raw: string | null | undefined): TrackingTab {
  return raw && TRACKING_TAB_VALUES.has(raw)
    ? (raw as TrackingTab)
    : DEFAULT_TRACKING_TAB
}

/** 从 URLSearchParams 解析复盘页面状态 */
export function decodeReviewUrl(params: URLSearchParams): ReviewUrlState {
  return {
    date: params.get('date'),
    stage: normalizeStage(params.get('stage')),
    scopeType: params.get('scopeType') || null,
    scopeKey: params.get('scopeKey') || null,
    signalId: params.get('signalId') || null,
    boardId: params.get('boardId') || null,
    symbol: params.get('symbol') || null,
    trackingTab: normalizeTrackingTab(params.get('trackingTab')),
  }
}

/** 将复盘状态编码为 URLSearchParams（仅写入非默认值） */
export function encodeReviewUrl(state: ReviewUrlState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.date) {
    params.set('date', state.date)
  }
  if (state.stage !== DEFAULT_REVIEW_STAGE) {
    params.set('stage', state.stage)
  }
  if (state.scopeType) {
    params.set('scopeType', state.scopeType)
  }
  if (state.scopeKey) {
    params.set('scopeKey', state.scopeKey)
  }
  if (state.signalId) {
    params.set('signalId', state.signalId)
  }
  if (state.boardId) {
    params.set('boardId', state.boardId)
  }
  if (state.symbol) {
    params.set('symbol', state.symbol)
  }
  if (state.trackingTab !== DEFAULT_TRACKING_TAB) {
    params.set('trackingTab', state.trackingTab)
  }
  return params
}

/** 基于现有状态部分更新字段，返回新状态对象（纯函数） */
export function patchReviewUrl(
  prev: ReviewUrlState,
  patch: Partial<ReviewUrlState>,
): ReviewUrlState {
  return { ...prev, ...patch }
}

/** 构建 /review?... 完整 URL（用于 navigate） */
export function buildReviewUrl(state: ReviewUrlState): string {
  const params = encodeReviewUrl(state)
  const qs = params.toString()
  return qs ? `/review?${qs}` : '/review'
}
