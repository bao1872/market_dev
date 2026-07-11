// [ResearchContextPanel] - 描述: 普通用户右侧研究上下文面板（主容器）
// 普通用户面板只渲染：
//   - EventExplanationCard（event_id 存在时）
//   - LatestEventCard（无 event_id 但有最新事件时）
//   - StructureSummaryCard（结构状态人类可读总结）
//   - KeyRangeCard（关键价格区间人类可读总结）
// 不显示内部字段名、算法参数、JSON 或商业机密。
// 原始 factor/feature/JSON 仅在 /admin/stocks/:symbol/debug 的 AdminStockDebugPage 中展示。
// 面板关闭时由父组件不渲染本组件，所有查询 enabled=false，不发请求。
// useResearchContext 是 event/structural/temporal 唯一查询入口；子组件只接收 data/query 状态，不再次调用 hooks。
import { useResearchContext } from './useResearchContext'
import { EventExplanationCard } from './EventExplanationCard'
import { StructureSummaryCard } from './StructureSummaryCard'
import { LatestEventCard } from './LatestEventCard'
import { KeyRangeCard } from './KeyRangeCard'
import type { StrategyEvent } from '@/api/endpoints'
import styles from './ResearchContext.module.scss'

export type ResearchContextStyles = typeof styles

interface ResearchContextPanelProps {
  instrumentId: string | undefined
  eventId: string | null | undefined
  /** 最新事件（来自 useStockResearchData 的 events，无 event_id 时展示） */
  latestEvent?: StrategyEvent | null
}

export function ResearchContextPanel({
  instrumentId,
  eventId,
  latestEvent,
}: ResearchContextPanelProps) {
  const { eventDetail, structural, temporal } = useResearchContext({
    instrumentId,
    eventId,
    enabled: true, // 本组件只在面板打开时渲染，故始终 enabled
  })

  return (
    <div className={styles.panel}>
      {/* 普通用户：事件解释（event_id 存在时） */}
      {eventId && (
        <EventExplanationCard query={eventDetail} styles={styles} currentInstrumentId={instrumentId} />
      )}

      {/* 普通用户：最新事件摘要（无 event_id 但有最新事件时） */}
      {!eventId && latestEvent && (
        <LatestEventCard event={latestEvent} styles={styles} />
      )}

      {/* 普通用户：关键价格区间 */}
      <KeyRangeCard structuralQuery={structural} styles={styles} />

      {/* 普通用户：结构状态摘要 */}
      <StructureSummaryCard
        structuralQuery={structural}
        temporalQuery={temporal}
        styles={styles}
      />
    </div>
  )
}
