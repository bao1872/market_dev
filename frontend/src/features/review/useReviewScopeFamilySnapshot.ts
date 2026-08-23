// [useReviewScopeFamilySnapshot] - 描述: 完整 family snapshot 加载（Slice D）
// 只做 transport aggregation：拉全部分页并按 (scopeType, scopeKey) 校验 identity 唯一，
// 不重算业务指标、不按行请求 detail、不派生 phase/score。
// 缓存键：tradeDate + family（React Query；不放 Zustand）。
// 纯加载逻辑 loadFamilySnapshot 可被 node --test 直接测试。
import { useQuery } from '@tanstack/react-query'
import { getReviewScopes } from './api'
import { reviewKeys } from './queryKeys'
import { REVIEW_MAX_PAGE_SIZE } from './urlState'
import type {
  ReviewScopeFamily,
  ReviewScopeListItem,
  ReviewScopeListResponse,
} from './types'

export interface ReviewScopeFamilySnapshot {
  items: ReviewScopeListItem[]
  total: number
  pageCount: number
}

export const FAMILY_SNAPSHOT_PAGE_SIZE = REVIEW_MAX_PAGE_SIZE

/**
 * 纯 transport aggregation：
 * 1. fetch page 1（page_size=100）
 * 2. total > 100 时，其余页并行请求（Promise.all）
 * 3. 按传输顺序合并（page 1 → 2 → ...）
 * 4. completeness fail closed：所有页 total 必须一致；合并 items.length 必须等于 total，
 *    否则抛错（绝不把部分快照当作完整快照渲染——搜索/phase/readiness 必须作用在完整 family 上）
 * 5. 校验 (scopeType, scopeKey) 无重复，重复则抛错（fail closed）
 */
export async function loadFamilySnapshot(
  fetchPage: (page: number) => Promise<ReviewScopeListResponse>,
  pageSize: number = FAMILY_SNAPSHOT_PAGE_SIZE,
): Promise<ReviewScopeFamilySnapshot> {
  const first = await fetchPage(1)
  const total = first.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const pages: ReviewScopeListResponse[] = [first]
  if (pageCount > 1) {
    const remaining = await Promise.all(
      Array.from({ length: pageCount - 1 }, (_, i) => fetchPage(i + 2)),
    )
    pages.push(...remaining)
  }

  // completeness：所有页 total 必须与 first.total 一致
  for (const p of pages) {
    const pTotal = p.total ?? 0
    if (pTotal !== total) {
      throw new Error(
        `family snapshot total 漂移: first=${total} page${p.page}=${pTotal}`,
      )
    }
  }

  const items = pages.flatMap((p) => p.items)

  // completeness：合并项数必须等于 total，否则 fail closed（HTTP 200 但数据缺失）
  if (items.length !== total) {
    throw new Error(`incomplete family snapshot: expected=${total} actual=${items.length}`)
  }

  const seen = new Set<string>()
  for (const item of items) {
    const id = `${item.scopeType}:${item.scopeKey}`
    if (seen.has(id)) {
      throw new Error(`family snapshot 含重复 scope identity: ${id}`)
    }
    seen.add(id)
  }

  return { items, total, pageCount }
}

/** 按 tradeDate + family 拉取完整 family snapshot（React Query 缓存） */
export function useReviewScopeFamilySnapshot(
  tradeDate: string | null,
  family: ReviewScopeFamily,
) {
  return useQuery({
    queryKey: reviewKeys.familySnapshot(tradeDate ?? '', family),
    queryFn: () =>
      loadFamilySnapshot((page) =>
        getReviewScopes(tradeDate ?? '', {
          scope_type: family,
          page,
          page_size: FAMILY_SNAPSHOT_PAGE_SIZE,
        }),
      ),
    enabled: !!tradeDate,
    staleTime: 5 * 60 * 1000,
  })
}
