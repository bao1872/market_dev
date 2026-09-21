// [R3C] /boards/:boardId 兼容 resolver（frontend-only）：
// 用最小窗口 detail API 解析 type，重定向到正确的 canonical Explorer；
// 失败 / 无 projection 时保留诚实迁移提示页（不恢复旧 Board Analysis API、不按 UUID 猜 type）。
import { Navigate } from 'react-router-dom'
import { useMarketScopeDetail } from '@/hooks/useMarketDashboardApi'
import BoardAnalysisRetiredPage from '@/pages/BoardAnalysisRetiredPage'

export default function BoardCompatResolver({ boardId }: { boardId: string }) {
  const detail = useMarketScopeDetail(boardId, 1)

  if (detail.isLoading) return <BoardAnalysisRetiredPage />
  if (detail.isError || !detail.data) return <BoardAnalysisRetiredPage />

  const meta = detail.data.metadata
  const target =
    meta.type === 'industry'
      ? `/review/industry?hierarchy_level=${meta.hierarchy_level}&board_id=${encodeURIComponent(meta.board_id)}`
      : `/review/concept?board_id=${encodeURIComponent(meta.board_id)}`
  return <Navigate to={target} replace />
}
