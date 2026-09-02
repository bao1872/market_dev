// [AuctionScopeViewModel] - 描述: V3.2 Scope Observation 视图模型（PRD V3.2 §47）
//
// 硬契约（prompt + backend AuctionScopeListItemOut）：
// - 纯函数：filter / sort / paginate，前端绝不重算业务指标（canonical owner 在后端 payload）。
// - 列排序 null 永远最后（升序与降序皆然），不把 null 当 0、不插值、不 carry。
// - family 切换为最高层级约束；切换后 preset / sort 保持，但 cohort 由后端按 family 重算。
// - 跨截面位置来自 cross_sectional 字典（axis → 0..100），concentration 轴常为空。
//
// 与后端 DTO 对齐说明（contract reconciliation，backend 已 frozen）：
// 列表 DTO 不暴露 top3_amount_share / repricingPosition 等旧草案字段；
// 跨截面位置在 crossSectional.{repricing,breadth,participation,concentration}。
import type {
  AuctionScopeListItemOut,
  AuctionScopeListOut,
} from './types'

/** 视图行 = 列表 DTO 的可空字段投影（与后端严格对齐）。 */
export interface AuctionScopeRow {
  scopeKey: string
  scopeName: string
  equalWeightGap: number | null
  amountWeightedGap: number | null
  capitalTilt: number | null
  positiveGapBreadth: number | null
  negativeGapBreadth: number | null
  unchangedGapBreadth: number | null
  gapDispersion: number | null
  priceNormalizedHhi: number | null
  ewPosition: number | null
  ewVelocity: number | null
  ewAcceleration: number | null
  amountHistoricalPosition: number | null
  amountMultiple: number | null
  amountAbnormalBreadth: number | null
  totalAuctionAmount: number | null
  normalizedHhi: number | null
  crossSectional: {
    repricing: number | null
    breadth: number | null
    participation: number | null
    concentration: number | null
  }
  leadershipMigration: number | null
  priceValidCount: number | null
}

/** 可排序字段（均为可空 numeric；scopeName 为文本兜底排序） */
export type AuctionScopeSortField =
  | 'scopeName'
  | 'equalWeightGap'
  | 'amountWeightedGap'
  | 'capitalTilt'
  | 'positiveGapBreadth'
  | 'negativeGapBreadth'
  | 'unchangedGapBreadth'
  | 'gapDispersion'
  | 'priceNormalizedHhi'
  | 'ewPosition'
  | 'ewVelocity'
  | 'ewAcceleration'
  | 'amountHistoricalPosition'
  | 'amountMultiple'
  | 'amountAbnormalBreadth'
  | 'totalAuctionAmount'
  | 'normalizedHhi'
  | 'leadershipMigration'
  | 'priceValidCount'
  | 'crossRepricing'
  | 'crossBreadth'
  | 'crossParticipation'
  | 'crossConcentration'

/** 把后端列表 DTO 投影为视图行。 */
export function toScopeRow(item: AuctionScopeListItemOut): AuctionScopeRow {
  return {
    scopeKey: item.scope_key,
    scopeName: item.scope_name,
    equalWeightGap: item.equal_weight_gap,
    amountWeightedGap: item.amount_weighted_gap,
    capitalTilt: item.capital_tilt,
    positiveGapBreadth: item.positive_gap_breadth,
    negativeGapBreadth: item.negative_gap_breadth,
    unchangedGapBreadth: item.unchanged_gap_breadth,
    gapDispersion: item.gap_dispersion,
    priceNormalizedHhi: item.price_normalized_hhi,
    ewPosition: item.ew_position,
    ewVelocity: item.ew_velocity,
    ewAcceleration: item.ew_acceleration,
    amountHistoricalPosition: item.amount_historical_position,
    amountMultiple: item.amount_multiple,
    amountAbnormalBreadth: item.amount_abnormal_breadth,
    totalAuctionAmount: item.total_auction_amount,
    normalizedHhi: item.normalized_hhi,
    crossSectional: {
      repricing: item.cross_sectional?.repricing ?? null,
      breadth: item.cross_sectional?.breadth ?? null,
      participation: item.cross_sectional?.participation ?? null,
      concentration: item.cross_sectional?.concentration ?? null,
    },
    leadershipMigration: item.leadership_migration,
    priceValidCount: item.price_valid_count,
  }
}

export function toScopeRows(list: AuctionScopeListOut | null | undefined): AuctionScopeRow[] {
  if (!list?.scopes) return []
  return list.scopes.map(toScopeRow)
}

/** 取排序字段值；文本字段 scopeName 与跨截面（嵌套 crossSectional）单独处理。 */
function getFieldValue(row: AuctionScopeRow, field: AuctionScopeSortField): number | null {
  if (field === 'scopeName') return null
  if (field === 'crossRepricing') return row.crossSectional.repricing
  if (field === 'crossBreadth') return row.crossSectional.breadth
  if (field === 'crossParticipation') return row.crossSectional.participation
  if (field === 'crossConcentration') return row.crossSectional.concentration
  return row[field] as number | null
}

/**
 * null-last 比较（两个方向都保持 null 最末）：
 * - 两者皆 null → 相等
 * - 仅 a 为 null → a 排后面（返回 1，无论 asc/desc）
 * - 仅 b 为 null → b 排后面（返回 -1）
 * - 都有值 → 按方向比较
 */
export function compareNullableNumber(
  a: number | null,
  b: number | null,
  direction: 'asc' | 'desc',
): number {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  const cmp = a - b
  return direction === 'asc' ? cmp : -cmp
}

export interface AuctionScopePreset {
  id: string
  label: string
  /** 透明：展示该 preset 背后的 filter + sort 字段，避免黑盒评分 */
  description: string
  sort: AuctionScopeSortField
  secondary: AuctionScopeSortField
}

/** 6 个 preset = filter + sort（可见背后字段，非黑盒 score）。 */
export const AUCTION_PRESETS: readonly AuctionScopePreset[] = [
  {
    id: 'high-open',
    label: '高开幅度',
    description: 'sort: equalWeightGap ↓ · secondary: ewPosition ↓',
    sort: 'equalWeightGap',
    secondary: 'ewPosition',
  },
  {
    id: 'capital-tilt',
    label: '资金倾斜',
    description: 'sort: capitalTilt ↓ · secondary: amountWeightedGap ↓',
    sort: 'capitalTilt',
    secondary: 'amountWeightedGap',
  },
  {
    id: 'strong-trend',
    label: '强趋势',
    description: 'sort: ewPosition ↓ · secondary: ewVelocity ↓',
    sort: 'ewPosition',
    secondary: 'ewVelocity',
  },
  {
    id: 'concentration',
    label: '资金集中',
    description: 'sort: normalizedHhi ↓ · secondary: totalAuctionAmount ↓',
    sort: 'normalizedHhi',
    secondary: 'totalAuctionAmount',
  },
  {
    id: 'resonance',
    label: '板块共振',
    description: 'sort: crossParticipation ↓ · secondary: crossBreadth ↓',
    sort: 'crossParticipation',
    secondary: 'crossBreadth',
  },
  {
    id: 'leadership-migration',
    label: '龙头迁移',
    description: 'sort: leadershipMigration ↓ · secondary: ewPosition ↓',
    sort: 'leadershipMigration',
    secondary: 'ewPosition',
  },
] as const

export interface AuctionScopeViewOptions {
  search?: string
  presetId?: string | null
  sort?: AuctionScopeSortField
  direction?: 'asc' | 'desc'
  page?: number
  pageSize?: number
}

export interface AuctionScopeView {
  rows: AuctionScopeRow[]
  total: number
  page: number
  pageSize: number
  pageCount: number
}

const DEFAULT_PAGE_SIZE = 50

/**
 * 纯函数视图装配（prompt §3.4 + §3.5）：
 * 1. search：scopeName 不区分大小写子串过滤。
 * 2. 排序：explicit sort > preset.sort；secondary 作为稳定 tie-break。
 * 3. 对整个 family snapshot 排序（前端本地，后端已返回完整 snapshot，无 Top-N）。
 * 4. paginate。
 * 全程 null-last。
 */
export function buildAuctionScopeView(
  rows: readonly AuctionScopeRow[],
  options: AuctionScopeViewOptions = {},
): AuctionScopeView {
  const {
    search = '',
    presetId = null,
    sort = null,
    direction = 'desc',
    page = 1,
    pageSize = DEFAULT_PAGE_SIZE,
  } = options

  const query = search.trim().toLowerCase()
  let filtered = rows
  if (query) {
    filtered = rows.filter((r) => r.scopeName.toLowerCase().includes(query))
  }

  const preset = presetId ? AUCTION_PRESETS.find((p) => p.id === presetId) : null
  const primary: AuctionScopeSortField = sort ?? preset?.sort ?? 'equalWeightGap'
  const secondary: AuctionScopeSortField = preset?.secondary ?? 'ewPosition'

  const sorted = [...filtered].sort((a, b) => {
    const pa = getFieldValue(a, primary)
    const pb = getFieldValue(b, primary)
    const c1 = compareNullableNumber(pa, pb, direction)
    if (c1 !== 0) return c1
    const sa = getFieldValue(a, secondary)
    const sb = getFieldValue(b, secondary)
    const c2 = compareNullableNumber(sa, sb, direction)
    if (c2 !== 0) return c2
    return a.scopeName.localeCompare(b.scopeName, 'zh-Hans-CN')
  })

  const total = sorted.length
  const safePageSize = pageSize > 0 ? pageSize : DEFAULT_PAGE_SIZE
  const pageCount = Math.max(1, Math.ceil(total / safePageSize))
  const safePage = Math.min(Math.max(1, page), pageCount)
  const start = (safePage - 1) * safePageSize
  const pageRows = sorted.slice(start, start + safePageSize)

  return {
    rows: pageRows,
    total,
    page: safePage,
    pageSize: safePageSize,
    pageCount,
  }
}

/** 在完整 rows 中定位当前选中 scope（用于高亮 + 右侧详情）。 */
export function resolveSelectedScope(
  rows: readonly AuctionScopeRow[],
  scopeKey: string | null | undefined,
): AuctionScopeRow | null {
  if (!scopeKey) return null
  return rows.find((r) => r.scopeKey === scopeKey) ?? null
}

// ===== 展示格式化（只读，不重算） =====

/** ratio → 百分比字符串（后端 equal_weight_gap 为 0.023 表示 +2.3%） */
export function formatRatioAsPercent(value: number | null, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const pct = value * 100
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(digits)}%`
}

export function formatNumber(value: number | null, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(digits)
}
