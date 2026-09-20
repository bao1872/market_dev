// [MarketDashboard] - 纯逻辑（可单测，不依赖 React / axios 实例，也不反向 import api 层）
import type { ScopeType, HierarchyLevel } from './types'

/** 0.6321 -> "63.2%"，null -> "—"（绝不显示 0%） */
export function formatBreadth(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—'
  return `${(ratio * 100).toFixed(1)}%`
}

/** 104.28 -> "104.28"，null -> "—" */
export function formatEwIndex(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(2)
}

/** 0.05 -> "+5.0%"，-0.03 -> "-3.0%"，null -> "—" */
export function formatDelta(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—'
  const pct = ratio * 100
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

export type DeltaDirection = 'up' | 'down' | 'flat'
/** A 股约定：正=红(up)、负=绿(down)、0/null=中性(flat) */
export function deltaDirection(value: number | null | undefined): DeltaDirection {
  if (value === null || value === undefined || Number.isNaN(value)) return 'flat'
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'flat'
}

/**
 * 把数据点序列转成图表 setData 数组。
 * 硬合同：null / undefined / NaN 不进入数组（lightweight-charts 表现为 whitespace gap = 断线），
 * 绝不补 0、绝不 forward fill、绝不插值。
 */
export function buildLineData<T>(
  data: readonly T[],
  timeField: keyof T,
  valueField: keyof T,
): Array<{ time: string; value: number }> {
  const out: Array<{ time: string; value: number }> = []
  for (const row of data) {
    const v = row[valueField]
    if (typeof v === 'number' && Number.isFinite(v)) {
      out.push({ time: String(row[timeField]), value: v })
    }
  }
  return out
}

/** 构造 rankings 请求参数；concept 不传 hierarchy_level，industry 仅在明确层级时传。 */
export function buildRankingsParams(
  scopeType: ScopeType,
  hierarchyLevel: HierarchyLevel | null,
): { scope_type: ScopeType; hierarchy_level?: HierarchyLevel; lookback: number; limit: number } {
  const params: { scope_type: ScopeType; hierarchy_level?: HierarchyLevel; lookback: number; limit: number } = {
    scope_type: scopeType,
    lookback: 5,
    limit: 10,
  }
  if (scopeType === 'industry' && hierarchyLevel) {
    params.hierarchy_level = hierarchyLevel
  }
  return params
}

export const MAX_COMPARE_BOARDS = 20

/** 比较选择上限校验；industry / concept 混合不被拒绝（前端不人为禁止）。 */
export function validateCompareSelection(ids: string[]): { ok: boolean; message?: string } {
  if (ids.length > MAX_COMPARE_BOARDS) {
    return { ok: false, message: `最多比较 ${MAX_COMPARE_BOARDS} 个板块` }
  }
  return { ok: true }
}

export type DashboardErrorKind = 'forbidden' | 'not-found' | 'error'

export interface ClassifiedError {
  kind: DashboardErrorKind
  detail: string
  requestId?: string | null
}

/** 已提取的错误（来自 api 层 extractMarketDashboardError）归类为前端状态语义；
 *  403 单独成 forbidden（不伪装成数据加载失败）。页面传入 extractMarketDashboardError(err)。 */
export function classifyDashboardError(e: {
  status?: number | null
  detail?: string | null
  message: string
  requestId?: string | null
}): ClassifiedError {
  if (e.status === 403) return { kind: 'forbidden', detail: e.detail ?? '' }
  if (e.status === 404) return { kind: 'not-found', detail: e.detail ?? '' }
  return { kind: 'error', detail: e.message }
}
