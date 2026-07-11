// [useResearchContext] - 描述: 右侧研究上下文面板的数据聚合 hook
// 聚合三个数据源：事件详情（useStrategyEventDetail）、结构因子（useStructuralFactors）、时序特征（useTemporalFeatures）
// 面板关闭时（enabled=false）传 undefined 给底层 hook，使其 enabled=false，不发请求。
// 优先复用现有 hooks，不新增后端算法或接口。
import { useStrategyEventDetail, useStructuralFactors, useTemporalFeatures } from '@/hooks/useApi'

export interface UseResearchContextParams {
  instrumentId: string | undefined
  eventId: string | null | undefined
  /** 面板是否可见（关闭时传 false，底层 hook enabled=false，不发请求） */
  enabled: boolean
}

export interface UseResearchContextResult {
  eventDetail: ReturnType<typeof useStrategyEventDetail>
  structural: ReturnType<typeof useStructuralFactors>
  temporal: ReturnType<typeof useTemporalFeatures>
}

/**
 * 聚合事件详情 + 结构因子 + 时序特征查询。
 * 面板关闭时（enabled=false）所有查询 enabled=false，不发请求、不挂载。
 * eventId 存在时才查事件详情；instrumentId 存在时才查结构/时序。
 */
export function useResearchContext(params: UseResearchContextParams): UseResearchContextResult {
  const eventDetail = useStrategyEventDetail(
    params.enabled ? params.eventId ?? undefined : undefined,
  )
  const structural = useStructuralFactors(
    params.enabled ? params.instrumentId : undefined,
  )
  const temporal = useTemporalFeatures(
    params.enabled ? params.instrumentId : undefined,
  )
  return { eventDetail, structural, temporal }
}
