// [ResearchContextPanel] - 描述: 右侧研究上下文面板（主容器）
// 普通用户：event_id 存在时显示事件解释卡片；无 event_id 时显示结构状态摘要卡片。
//   不显示内部字段名、算法参数、JSON 或商业机密。
// 管理员：debug=true 时额外显示 AdminFactorDebugPanel（原始 factor/feature/JSON）。
//   普通用户即使手工加 debug=1 也不可见（由父组件 MarketWorkspacePage 校验 is_admin 后传 debug）。
// 面板关闭时由父组件不渲染本组件，所有查询 enabled=false，不发请求。
// 同时复用现有 StockStructuralStatePanel（详细因子卡片），保持 active swing 与 Capture 布局语义。
import { useResearchContext } from './useResearchContext'
import { EventExplanationCard } from './EventExplanationCard'
import { StructureSummaryCard } from './StructureSummaryCard'
import { AdminFactorDebugPanel } from './AdminFactorDebugPanel'
import { StockStructuralStatePanel } from '@/components/StockStructuralStatePanel'
import styles from './ResearchContext.module.scss'

export type ResearchContextStyles = typeof styles

interface ResearchContextPanelProps {
  instrumentId: string | undefined
  eventId: string | null | undefined
  /** debug=1 且管理员时为 true（由父组件校验 is_admin） */
  debug: boolean
}

export function ResearchContextPanel({
  instrumentId,
  eventId,
  debug,
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
        <EventExplanationCard query={eventDetail} styles={styles} />
      )}

      {/* 普通用户：结构状态摘要（始终展示，无 event_id 时作为主要内容） */}
      <StructureSummaryCard
        structuralQuery={structural}
        temporalQuery={temporal}
        styles={styles}
      />

      {/* 管理员调试面板（仅 debug=true） */}
      {debug && (
        <AdminFactorDebugPanel
          eventDetailQuery={eventDetail}
          structuralQuery={structural}
          temporalQuery={temporal}
          eventId={eventId}
          styles={styles}
        />
      )}

      {/* 详细因子卡片（复用现有组件，保持 active swing 与 Capture 布局语义） */}
      {instrumentId && (
        <StockStructuralStatePanel instrumentId={instrumentId} debug={debug} />
      )}
    </div>
  )
}
