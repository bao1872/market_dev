// [EventExplanationCard] - 描述: 普通用户事件解释卡片
// 当 event_id 存在时展示事件时间、类型（通俗文案）、关键证据、关联价格。
// 不显示内部字段名、算法参数、JSON 或商业机密。
// 错误/不存在时明确提示。
import type { UseQueryResult } from '@tanstack/react-query'
import type { StrategyEventDetail } from '@/api/endpoints'
import { getEventLabel } from '@/constants/userFacingLabels'
import { formatShanghaiTime } from '@/utils/datetime'
import type { ResearchContextStyles } from './ResearchContextPanel'

interface EventExplanationCardProps {
  query: UseQueryResult<StrategyEventDetail, Error>
  styles: ResearchContextStyles
}

// 从 payload 中提取关联价格（三级回退：facts → 顶层字段 → 纯文本正则）
function extractEventPrice(payload: Record<string, unknown>): string | null {
  // 1. facts 数组按 key 匹配
  const facts = payload.facts as Array<Record<string, unknown>> | undefined
  if (Array.isArray(facts)) {
    for (const f of facts) {
      const k = String(f.key ?? '').toLowerCase()
      if (k === 'current_price' || k === 'price' || k === '现价') {
        const v = f.value
        if (v !== undefined && v !== null && v !== '') return String(v)
      }
    }
  }
  // 2. 顶层结构化字段
  const directKeys = ['current_price', 'price', 'last_price', 'close_price']
  for (const k of directKeys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return String(v)
  }
  // 3. 纯文本正则
  const text = payload.text_content as string | undefined
  if (text) {
    const m = text.match(/现价[：:]\s*([\d.]+)/)
    if (m) return m[1]
  }
  return null
}

// 从 payload 中提取关键证据（通俗描述，不暴露内部字段名）
function extractEvidence(payload: Record<string, unknown>): string[] {
  const evidence: string[] = []
  // text_content / summary 通常含人类可读描述
  const text = payload.text_content as string | undefined
  if (text) {
    evidence.push(text)
  }
  const summary = payload.summary as string | undefined
  if (summary && summary !== text) {
    evidence.push(summary)
  }
  return evidence
}

export function EventExplanationCard({ query, styles }: EventExplanationCardProps) {
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

  const detail = query.data
  if (!detail) {
    return (
      <div className={styles.section}>
        <div className={styles.sectionTitle}>事件解释</div>
        <div className={styles.empty}>未找到该事件。</div>
      </div>
    )
  }

  const eventLabel = getEventLabel(detail.event_type)
  const eventTime = formatShanghaiTime(detail.event_time)
  const price = extractEventPrice(detail.payload)
  const evidence = extractEvidence(detail.payload)

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
          <span className={styles.eventValue}>{eventLabel}</span>
        </div>
        {price && (
          <div className={styles.eventRow}>
            <span className={styles.eventLabel}>当时价格</span>
            <span className={styles.eventValue}>{price}</span>
          </div>
        )}
        {evidence.length > 0 && (
          <div className={styles.evidenceBlock}>
            {evidence.map((text, i) => (
              <p key={i} className={styles.evidenceText}>{text}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
