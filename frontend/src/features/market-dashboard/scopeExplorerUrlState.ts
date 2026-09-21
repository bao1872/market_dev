// [R3C] Explorer URL-state 纯模块（无 React 依赖，可由 node:test 直接加载）。
//
// URL search params 是 Explorer 全部 applied server state 的 SSOT：
//   hierarchy_level / board_id / q / page / page_size / sort / direction / 全部 numeric filters
// 解析非法 → 归位默认值；序列化 canonical（缺省值也显式写出，刷新可完整恢复）。
// concept 永远不带 hierarchy_level（解析时忽略、序列化时不输出）。

import type { HierarchyLevel, ScopeType } from './types'
import {
  SCOPE_EXPLORER_PAGE_SIZE_DEFAULT,
  SCOPE_EXPLORER_PAGE_SIZE_MAX,
  SCOPE_EXPLORER_SORT_DEFAULT,
  SCOPE_EXPLORER_SORT_FIELDS,
  SCOPE_EXPLORER_DIRECTION_DEFAULT,
  type ScopeExplorerSort,
  type SortDirection,
} from './scopeExplorerQuery'

export type NumericFilterKey =
  | 'ma5_min'
  | 'ma5_max'
  | 'ma10_min'
  | 'ma10_max'
  | 'ma5_delta_min'
  | 'ma5_delta_max'
  | 'ma10_delta_min'
  | 'ma10_delta_max'
  | 'member_count_min'
  | 'member_count_max'

export const NUMERIC_FILTER_KEYS: readonly NumericFilterKey[] = [
  'ma5_min',
  'ma5_max',
  'ma10_min',
  'ma10_max',
  'ma5_delta_min',
  'ma5_delta_max',
  'ma10_delta_min',
  'ma10_delta_max',
  'member_count_min',
  'member_count_max',
]

export const INDUSTRY_LEVELS: readonly HierarchyLevel[] = ['L1', 'L2', 'L3']
export const EXPLORER_PAGE_SIZE_OPTIONS = [20, 50, 100] as const

// [R3C] 表格列顺序锁死（与表头渲染一致）：行业/概念 · 成员数 · MA5 · 5日Δ · MA10 · 5日Δ · MA20 · MA50 · MA120 · 操作。
// 放到纯模块，供 ScopeExplorerTable（含 .scss）与契约测试同时无副作用导入。
export const EXPLORER_TABLE_COLUMNS = [
  'name',
  'member_count',
  'ma5',
  'ma5_delta',
  'ma10',
  'ma10_delta',
  'ma20',
  'ma50',
  'ma120',
  'operation',
] as const

export interface ExplorerParsed {
  hierarchy_level: HierarchyLevel
  board_id: string | null
  q: string
  page: number
  page_size: number
  sort: ScopeExplorerSort
  direction: SortDirection
  filters: Record<NumericFilterKey, number | null>
}

export type ExplorerStatePatch = Partial<{
  hierarchy_level: HierarchyLevel
  board_id: string | null
  q: string
  page: number
  page_size: number
  sort: ScopeExplorerSort
  direction: SortDirection
  filters: Partial<Record<NumericFilterKey, number | null>>
}>

// ---------------------------------------------------------------------------
// 单位转换：UI（用户可见 % / pp / 整数）↔ backend（ratio）
// ---------------------------------------------------------------------------
export function breadthPctToRatio(pct: number): number {
  return pct / 100
}
export function breadthRatioToPct(ratio: number): number {
  return Math.round(ratio * 100)
}
export function deltaPpToRatio(pp: number): number {
  return pp / 100
}
export function deltaRatioToPp(ratio: number): number {
  return Math.round(ratio * 100)
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------
function parsePositiveInt(raw: string | null, fallback: number, max?: number): number {
  if (raw === null) return fallback
  const n = Number.parseInt(raw, 10)
  if (!Number.isInteger(n) || n < 1) return fallback
  if (max !== undefined && n > max) return max
  return n
}

function parseSort(raw: string | null): ScopeExplorerSort {
  return SCOPE_EXPLORER_SORT_FIELDS.includes((raw ?? '') as ScopeExplorerSort)
    ? (raw as ScopeExplorerSort)
    : SCOPE_EXPLORER_SORT_DEFAULT
}

function parseDirection(raw: string | null): SortDirection {
  return raw === 'asc' || raw === 'desc' ? raw : SCOPE_EXPLORER_DIRECTION_DEFAULT
}

function parseHierarchy(raw: string | null): HierarchyLevel {
  return INDUSTRY_LEVELS.includes((raw ?? '') as HierarchyLevel) ? (raw as HierarchyLevel) : 'L1'
}

function parseFilter(raw: string | null): number | null {
  if (raw === null || raw.trim() === '') return null
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) ? n : null
}

function emptyFilters(): Record<NumericFilterKey, number | null> {
  return NUMERIC_FILTER_KEYS.reduce(
    (acc, k) => {
      acc[k] = null
      return acc
    },
    {} as Record<NumericFilterKey, number | null>,
  )
}

function parseFilters(params: URLSearchParams): Record<NumericFilterKey, number | null> {
  const out = emptyFilters()
  for (const k of NUMERIC_FILTER_KEYS) out[k] = parseFilter(params.get(k))
  return out
}

function parseCommon(params: URLSearchParams): Omit<ExplorerParsed, 'hierarchy_level'> {
  return {
    board_id: params.get('board_id') ?? null,
    q: (params.get('q') ?? '').trim(),
    page: parsePositiveInt(params.get('page'), 1),
    page_size: parsePositiveInt(params.get('page_size'), SCOPE_EXPLORER_PAGE_SIZE_DEFAULT, SCOPE_EXPLORER_PAGE_SIZE_MAX),
    sort: parseSort(params.get('sort')),
    direction: parseDirection(params.get('direction')),
    filters: parseFilters(params),
  }
}

export function parseIndustryExplorerSearch(params: URLSearchParams): ExplorerParsed {
  return { hierarchy_level: parseHierarchy(params.get('hierarchy_level')), ...parseCommon(params) }
}

export function parseConceptExplorerSearch(params: URLSearchParams): ExplorerParsed {
  // concept 永远没有层级：URL 上若带 hierarchy_level 直接忽略（由页面规范化剥离）。
  return { hierarchy_level: 'L1', ...parseCommon(params) }
}

// ---------------------------------------------------------------------------
// 序列化（canonical：缺省值也显式写出，空 q 不写；concept 不带 hierarchy_level）
// ---------------------------------------------------------------------------
export function serializeExplorerState(state: ExplorerParsed, scopeType: ScopeType): URLSearchParams {
  const p = new URLSearchParams()
  if (scopeType === 'industry') p.set('hierarchy_level', state.hierarchy_level)
  if (state.board_id) p.set('board_id', state.board_id)
  if (state.q) p.set('q', state.q)
  p.set('page', String(state.page))
  p.set('page_size', String(state.page_size))
  p.set('sort', state.sort)
  p.set('direction', state.direction)
  for (const k of NUMERIC_FILTER_KEYS) {
    const v = state.filters[k]
    if (v !== null) p.set(k, String(v))
  }
  return p
}

// ---------------------------------------------------------------------------
// 状态更新：除显式改 page 外，任何 server-state 变化都重置到第 1 页。
// ---------------------------------------------------------------------------
export function updateExplorerState(prev: ExplorerParsed, patch: ExplorerStatePatch): ExplorerParsed {
  const next: ExplorerParsed = {
    ...prev,
    ...patch,
    filters: { ...prev.filters, ...(patch.filters ?? {}) },
  }
  // 层级切换使已选 board_id 失效（不同层级下的 board 不可比），必须清除。
  const levelChanged = patch.hierarchy_level !== undefined && patch.hierarchy_level !== prev.hierarchy_level
  if (levelChanged) next.board_id = null
  const resetPage = !('page' in patch) && Object.keys(patch).length > 0
  if (resetPage) next.page = 1
  return next
}

export function clearFiltersPatch(): ExplorerStatePatch {
  return {
    filters: NUMERIC_FILTER_KEYS.reduce(
      (acc, k) => {
        acc[k] = null
        return acc
      },
      {} as Record<NumericFilterKey, number | null>,
    ),
  }
}

// ---------------------------------------------------------------------------
// 快捷筛选（诚实标注 inclusive；后端 range 为 >= / <= 闭区间）
// ---------------------------------------------------------------------------
export interface FilterPreset {
  key: string
  label: string
  /** apply 时注入的 ratio 单位 patch（由页面经 updateExplorerState 写入）。 */
  filters: Partial<Record<NumericFilterKey, number | null>>
}

export const MA5_PRESETS: readonly FilterPreset[] = [
  { key: 'ma5_ge_80', label: 'MA5 ≥ 80%', filters: { ma5_min: breadthPctToRatio(80) } },
  { key: 'ma5_le_20', label: 'MA5 ≤ 20%', filters: { ma5_max: breadthPctToRatio(20) } },
  { key: 'ma5d_ge_0', label: 'MA5 Δ ≥ 0', filters: { ma5_delta_min: deltaPpToRatio(0) } },
  { key: 'ma5d_le_0', label: 'MA5 Δ ≤ 0', filters: { ma5_delta_max: deltaPpToRatio(0) } },
]

// ---------------------------------------------------------------------------
// UI 草稿（字符串输入）↔ ratio 双向
// ---------------------------------------------------------------------------
export type FilterDraft = Record<NumericFilterKey, string>

export function ratioToUi(key: NumericFilterKey, value: number | null): string {
  if (value === null) return ''
  if (key.startsWith('member_count')) return String(value)
  return String(Math.round(value * 100))
}

export function uiToRatio(key: NumericFilterKey, raw: string): number | null {
  if (raw.trim() === '') return null
  const n = Number.parseFloat(raw)
  if (!Number.isFinite(n)) return null
  return key.startsWith('member_count') ? n : n / 100
}
