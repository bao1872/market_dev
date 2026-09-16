// Board Analysis 领域 hooks owner（[S3-E] 由 useApi.ts 迁出；read-only）。

import { useQuery } from '@tanstack/react-query'
import * as api from '../api/boardAnalysis'

export function useBoardAnalysisList(
  params?: api.BoardAnalysisListParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['board-analysis', 'list', params],
    queryFn: ({ signal }) => api.getBoardAnalysisList(params, { signal }),
    staleTime: 60 * 1000, // 1 分钟
    enabled: options?.enabled ?? true,
  })
}

/** 查询单板块分析详情 */
export function useBoardAnalysisDetail(
  boardId: string | null,
  params?: { trade_date?: string },
) {
  return useQuery({
    queryKey: ['board-analysis', 'detail', boardId, params],
    queryFn: ({ signal }) =>
      api.getBoardAnalysisDetail(boardId!, params, { signal }),
    enabled: !!boardId,
    staleTime: 60 * 1000,
  })
}

/** [Admin] 触发单板块分析计算 */

/** [Admin] 触发批量板块分析计算 */

// [S3-B] useAddToWatchlist / useRemoveFromWatchlist 已迁至 ./useWatchlistApi
// （见本文件 "Watchlist hooks" 兼容 re-export）。

// ============================================================

