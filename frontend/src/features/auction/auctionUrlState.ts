// [AuctionUrlState] - 描述: V3.2 Workspace URL 单一事实源（SSOT）
//
// URL 承载全部视图状态，支持刷新/前进/后退恢复：
//   trade_date / family / scope / sort / direction / search / preset / page
// family 是最高层级约束；切换后 sort/preset 保持，但 cohort 由后端按 family 重算。
import type { AuctionScopeSortField } from './auctionScopeViewModel'

export type AuctionFamily = 'industry' | 'concept'

export interface AuctionUrlState {
  tradeDate?: string
  family: AuctionFamily
  scope?: string
  sort?: AuctionScopeSortField
  direction: 'asc' | 'desc'
  search: string
  preset?: string | null
  page: number
}

export const DEFAULT_AUCTION_URL_STATE: AuctionUrlState = {
  family: 'industry',
  direction: 'desc',
  search: '',
  preset: null,
  page: 1,
}

function isFamily(v: string | null): v is AuctionFamily {
  return v === 'industry' || v === 'concept'
}

/** 从 URLSearchParams 解析视图状态（缺省回退默认值）。 */
export function parseAuctionUrlState(params: URLSearchParams): AuctionUrlState {
  const family = params.get('family')
  const direction = params.get('direction')
  const pageRaw = params.get('page')
  const sort = params.get('sort') as AuctionScopeSortField | null
  return {
    tradeDate: params.get('trade_date') ?? undefined,
    family: isFamily(family) ? family : DEFAULT_AUCTION_URL_STATE.family,
    scope: params.get('scope') ?? undefined,
    sort: sort ?? undefined,
    direction: direction === 'asc' ? 'asc' : 'desc',
    search: params.get('search') ?? '',
    preset: params.get('preset') ?? null,
    page: pageRaw ? Math.max(1, parseInt(pageRaw, 10) || 1) : 1,
  }
}

/** 把视图状态序列化为 URLSearchParams（省略默认值以减少 URL 噪声）。 */
export function serializeAuctionUrlState(state: AuctionUrlState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.tradeDate) params.set('trade_date', state.tradeDate)
  if (state.family !== DEFAULT_AUCTION_URL_STATE.family) params.set('family', state.family)
  if (state.scope) params.set('scope', state.scope)
  if (state.sort) params.set('sort', state.sort)
  if (state.direction !== 'desc') params.set('direction', state.direction)
  if (state.search) params.set('search', state.search)
  if (state.preset) params.set('preset', state.preset)
  if (state.page > 1) params.set('page', String(state.page))
  return params
}
