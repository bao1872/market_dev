// [EventExplanationCard] - 描述: 普通用户事件解释卡片
// 当 event_id 存在时展示事件时间、类型（通俗文案）、关键证据、关联价格。
// 不显示内部字段名、算法参数、JSON 或商业机密。
// 错误/不存在时明确提示。
// 数据提取由纯函数 buildUserEventExplanation 负责；本组件只负责渲染。
import type { UseQueryResult } from '@tanstack/react-query'
import type { StrategyEventDetail } from '@/api/endpoints'
import { formatShanghaiTime } from '@/utils/datetime'
import type { ResearchContextStyles } from './ResearchContextPanel'
import { buildUserEventExplanation } from './buildUserEventExplanation'

interface EventExplanationCardProps {
  query: UseQueryResult<StrategyEventDetail, Error>
  styles: ResearchContextStyles
  /** 当前查看的 instrumentId，用于校验 event 是否属于当前股票 */
  currentInstrumentId?: string | null
}

export function EventExplanationCard({ query, styles, currentInstrumentId }: EventExplanationCardProps) {
  if (query.isLoading) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>事件解释</div>
        <div className={styles.loading}>加载中…</div>
      </div>
    )
  }

  if (query.isError) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>事件解释</div>
        <div className={styles.error}>
          事件详情加载失败：{query.error?.message || '未知错误'}
        </div>
      </div>
    )
  }

  // 调用纯函数提取白名单字段（不直接整段输出任意 payload）
  const explanation = buildUserEventExplanation({
    eventDetail: query.data,
    currentInstrumentId,
  })

  if (!explanation.hasEvent) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>事件解释</div>
        <div className={styles.empty}>未找到该事件。</div>
      </div>
    )
  }

  const eventTime = explanation.eventTime ? formatShanghaiTime(explanation.eventTime) : '-'

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>事件解释</div>
      <div className={styles.eventCard}>
        <div className={styles.eventRow}>
          <span className={styles.eventLabel}>触发时间</span>
          <span className={styles.eventValue}>{eventTime}</span>
        </div>
        <div className={styles.eventRow}>
          <span className={styles.eventLabel}>事件类型</span>
          <span className={styles.eventValue}>{explanation.eventLabel}</span>
        </div>
        {explanation.price && !explanation.instrumentMismatch && (
          <div className={styles.eventRow}>
            <span className={styles.eventLabel}>当时价格</span>
            <span className={styles.eventValue}>{explanation.price}</span>
          </div>
        )}
        {explanation.instrumentMismatch && (
          <div className={styles.eventRow}>
            <span className={styles.eventLabel}>提示</span>
            <span className={styles.eventValue}>该事件属于其他股票，价格信息已隐藏</span>
          </div>
        )}
        {explanation.evidence.length > 0 && (
          <div className={styles.evidenceBlock}>
            {explanation.evidence.map((text, i) => (
              <p key={i} className={styles.evidenceText}>{text}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
