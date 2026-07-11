// [LatestEventCard] - 描述: 普通用户最新事件摘要卡片
// 无 event_id 但有最新事件时展示事件时间、类型（通俗文案）和简短描述。
// 不显示内部字段名、算法参数、JSON 或商业机密。
import type { StrategyEvent } from '@/api/endpoints'
import { getEventLabel } from '@/constants/userFacingLabels'
import { formatShanghaiTime } from '@/utils/datetime'
import type { ResearchContextStyles } from './ResearchContextPanel'

interface LatestEventCardProps {
  event: StrategyEvent
  styles: ResearchContextStyles
}

export function LatestEventCard({ event, styles }: LatestEventCardProps) {
  const eventLabel = getEventLabel(event.event_type)
  const eventTime = formatShanghaiTime(event.event_time)

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>最新事件</div>
      <div className={styles.eventCard}>
        <div className={styles.eventRow}>
          <span className={styles.eventLabel}>触发时间</span>
          <span className={styles.eventValue}>{eventTime}</span>
        </div>
        <div className={styles.eventRow}>
          <span className={styles.eventLabel}>事件类型</span>
          <span className={styles.eventValue}>{eventLabel}</span>
        </div>
      </div>
    </div>
  )
}
