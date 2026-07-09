// [ScreenerUrlState] - 描述: 趋势选股 URL 状态 encode/decode 工具函数
// 用法：将 strategy/keyword/sort/filters/page/pageSize 与 URLSearchParams 双向转换
//
// 设计约束：
// - filters 仅持久化 key/op/value/value2，不保存 rows/selectedKeys/activeRunId/results
// - decode 时按当前有效列 key 集合丢弃陈旧 filter/sort key
// - 默认 page=1 / pageSize=50 时省略，保持 URL 紧凑

export interface ScreenerUrlFilter {
  key: string
  op: string
  value?: unknown
  value2?: unknown
}

export interface ScreenerUrlSort {
  key: string
  direction: 'asc' | 'desc'
}

export interface ScreenerUrlState {
  strategy?: string
  keyword?: string
  sort?: ScreenerUrlSort
  filters?: ScreenerUrlFilter[]
  page?: number
  pageSize?: number
}

const DEFAULT_PAGE = 1
const DEFAULT_PAGE_SIZE = 50

/** 将趋势选股状态编码为 URLSearchParams */
export function encodeScreenerUrlState(state: ScreenerUrlState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.strategy) {
    params.set('strategy', state.strategy)
  }
  if (state.keyword?.trim()) {
    params.set('keyword', state.keyword.trim())
  }
  if (state.sort) {
    params.set('sort', state.sort.key)
    params.set('dir', state.sort.direction)
  }
  if (state.filters && state.filters.length > 0) {
    const compact = state.filters.map((f) => {
      const item: Record<string, unknown> = { key: f.key, op: f.op }
      if (f.value !== undefined) item.value = f.value
      if (f.value2 !== undefined) item.value2 = f.value2
      return item
    })
    params.set('filters', JSON.stringify(compact))
  }
  if (state.page !== undefined && state.page !== DEFAULT_PAGE) {
    params.set('page', String(state.page))
  }
  if (state.pageSize !== undefined && state.pageSize !== DEFAULT_PAGE_SIZE) {
    params.set('page_size', String(state.pageSize))
  }
  return params
}

/** 从 URLSearchParams 解码趋势选股状态；仅保留 validKeys 中存在的 filter/sort key */
export function decodeScreenerUrlState(
  params: URLSearchParams,
  validKeys: Set<string>,
): ScreenerUrlState {
  const state: ScreenerUrlState = {}

  const strategy = params.get('strategy')
  if (strategy) {
    state.strategy = strategy
  }

  const keyword = params.get('keyword')
  if (keyword) {
    state.keyword = keyword
  }

  const sortKey = params.get('sort')
  const sortDir = params.get('dir')
  if (sortKey && sortDir && validKeys.has(sortKey) && (sortDir === 'asc' || sortDir === 'desc')) {
    state.sort = { key: sortKey, direction: sortDir }
  }

  const filtersRaw = params.get('filters')
  if (filtersRaw) {
    try {
      const parsed: unknown = JSON.parse(filtersRaw)
      if (Array.isArray(parsed)) {
        state.filters = parsed
          .filter(
            (item): item is Record<string, unknown> =>
              item !== null && typeof item === 'object' && validKeys.has(String(item.key)),
          )
          .map((item) => {
            const filter: ScreenerUrlFilter = {
              key: String(item.key),
              op: String(item.op),
            }
            if (item.value !== undefined) filter.value = item.value
            if (item.value2 !== undefined) filter.value2 = item.value2
            return filter
          })
      }
    } catch {
      state.filters = []
    }
  }

  const page = params.get('page')
  if (page) {
    const n = parseInt(page, 10)
    if (!Number.isNaN(n) && n > 0) {
      state.page = n
    }
  }

  const pageSize = params.get('page_size')
  if (pageSize) {
    const n = parseInt(pageSize, 10)
    if (!Number.isNaN(n) && n > 0) {
      state.pageSize = n
    }
  }

  // 往返一致：缺失时填充默认值
  if (state.page === undefined) {
    state.page = DEFAULT_PAGE
  }
  if (state.pageSize === undefined) {
    state.pageSize = DEFAULT_PAGE_SIZE
  }

  return state
}
