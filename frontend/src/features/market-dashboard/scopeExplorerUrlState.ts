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

// ---------------------------------------------------------------------------
// [PANJI-REVIEW-UI-UNIFY] 表头筛选（funnel）元数据与派生
//
// 只有 backend 当前**真实支持**数值筛选的列才允许出现 funnel：
//   member_count / ma5 / ma5_delta / ma10 / ma10_delta
// MA20 / MA50 / MA120 没有对应 filter 参数 → 只可排序，绝不放「看起来能筛、
// 实际后端做不到」的假按钮。
// ---------------------------------------------------------------------------
export const EXPLORER_FILTERABLE_COLUMNS = [
  'member_count',
  'ma5',
  'ma5_delta',
  'ma10',
  'ma10_delta',
] as const

export type ExplorerFilterColumn = (typeof EXPLORER_FILTERABLE_COLUMNS)[number]

export interface ExplorerFilterColumnSpec {
  /** 表头显示 label（与锁死的列顺序一致；两个「5日Δ」列显示名相同） */
  label: string
  /** 筛选按钮可访问名（两个「5日Δ」列必须可区分） */
  filterLabel: string
  /** 单位：percent（ratio→%）/ pp（ratio→百分点）/ count（整数） */
  unit: 'percent' | 'pp' | 'count'
}

export const EXPLORER_FILTER_COLUMN_SPECS: Record<ExplorerFilterColumn, ExplorerFilterColumnSpec> = {
  member_count: { label: '成员数', filterLabel: '成员数', unit: 'count' },
  ma5: { label: 'MA5', filterLabel: 'MA5', unit: 'percent' },
  ma5_delta: { label: '5日Δ', filterLabel: 'MA5 5日Δ', unit: 'pp' },
  ma10: { label: 'MA10', filterLabel: 'MA10', unit: 'percent' },
  ma10_delta: { label: '5日Δ', filterLabel: 'MA10 5日Δ', unit: 'pp' },
}

export function filterKeysForColumn(column: ExplorerFilterColumn): {
  min: NumericFilterKey
  max: NumericFilterKey
} {
  return {
    min: `${column}_min` as NumericFilterKey,
    max: `${column}_max` as NumericFilterKey,
  }
}

// 列（sort field）→ 该列对应的数值筛选列。
// **只有出现在本表中的列才渲染 funnel** —— 这是「不制造假筛选」的可执行定义：
// 后端没有 ma20_min / ma50_min / ma120_min 参数，故 MA20/MA50/MA120 只可排序。
// 与 EXPLORER_TABLE_COLUMNS 同理放在纯模块，供表格组件与契约测试无副作用导入。
export const FILTER_COLUMN_BY_FIELD: Partial<Record<ScopeExplorerSort, ExplorerFilterColumn>> = {
  member_count: 'member_count',
  ma5: 'ma5',
  ma5_delta: 'ma5_delta',
  ma10: 'ma10',
  ma10_delta: 'ma10_delta',
}

export function isExplorerColumnFiltered(
  column: ExplorerFilterColumn,
  filters: Record<NumericFilterKey, number | null>,
): boolean {
  const { min, max } = filterKeysForColumn(column)
  return filters[min] !== null || filters[max] !== null
}

function formatExplorerFilterValue(column: ExplorerFilterColumn, value: number): string {
  const spec = EXPLORER_FILTER_COLUMN_SPECS[column]
  if (spec.unit === 'count') return String(value)
  const ui = Math.round(value * 100)
  return spec.unit === 'pp' ? `${ui}pp` : `${ui}%`
}

export interface ExplorerFilterChip {
  column: ExplorerFilterColumn
  /**
   * 用户可读条件，如 `MA5 60%–80%` / `成员数 ≥ 30` / `MA5 5日Δ ≥ 3pp`。
   * 用 `filterLabel` 拼装（不是列表头 `label`）——MA5/MA10 的 5日Δ 列标题相同，
   * 只有 filterLabel 能让用户分辨是哪一列的筛选。
   */
  label: string
  /** 该 chip 对应的 URL filter keys（清除时置空） */
  keys: NumericFilterKey[]
}

/** 当前 URL 上的激活数值筛选（按列聚合为一个 chip）。 */
export function activeExplorerFilterChips(
  filters: Record<NumericFilterKey, number | null>,
): ExplorerFilterChip[] {
  const chips: ExplorerFilterChip[] = []
  for (const column of EXPLORER_FILTERABLE_COLUMNS) {
    const { min, max } = filterKeysForColumn(column)
    const lo = filters[min]
    const hi = filters[max]
    if (lo === null && hi === null) continue
    const spec = EXPLORER_FILTER_COLUMN_SPECS[column]
    let range: string
    if (lo !== null && hi !== null) {
      range = `${formatExplorerFilterValue(column, lo)}–${formatExplorerFilterValue(column, hi)}`
    } else if (lo !== null) {
      range = `≥ ${formatExplorerFilterValue(column, lo)}`
    } else if (hi !== null) {
      range = `≤ ${formatExplorerFilterValue(column, hi)}`
    } else {
      continue
    }
    // chip 用 filterLabel 而非 label：MA5 与 MA10 的列标题同为「5日Δ」，
    // 只有 filterLabel 能区分（MA5 5日Δ / MA10 5日Δ），否则两条 chip 无法辨认。
    chips.push({ column, label: `${spec.filterLabel} ${range}`, keys: [min, max] })
  }
  return chips
}

// ---------------------------------------------------------------------------
// [PANJI-REVIEW-UI-UNIFY] 筛选弹层「条件」→ min/max 输入映射
//
// 当前 backend 只有 min/max 区间模型，因此 区间/≥/≤/= 四种 UI 条件最终都转换为
// ma5_min/ma5_max/... 两组 query param（业务 query contract 完全不变）。
// ---------------------------------------------------------------------------
export type ExplorerFilterMode = 'range' | 'gte' | 'lte' | 'eq'

export const EXPLORER_FILTER_MODES: readonly { value: ExplorerFilterMode; label: string }[] = [
  { value: 'range', label: '区间' },
  { value: 'gte', label: '≥' },
  { value: 'lte', label: '≤' },
  { value: 'eq', label: '=' },
]

export function draftFromFilterMode(
  mode: ExplorerFilterMode,
  lower: string,
  upper: string,
): { min: string; max: string } {
  const lo = lower.trim()
  const hi = upper.trim()
  if (mode === 'gte') return { min: lo, max: '' }
  if (mode === 'lte') return { min: '', max: hi }
  if (mode === 'eq') return { min: lo, max: lo }
  return { min: lo, max: hi }
}
