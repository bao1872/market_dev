// [SignalCard] - 描述: 信号卡片组件（PRD §14.4 阶段二）
// 必须显示：范围/信号类型/生命周期状态/首次出现日期+持续日数/触发变量/历史分位/coverage/结构化解释
// 操作：查看归因、查看历史、加入追踪
// 禁止显示黑箱总分；禁止自由 AI 结论（解释由模板根据结构化字段生成）
import type { ReviewSignal } from './types'
import styles from './review.module.scss'

const SIGNAL_STATUS_META: Record<string, { label: string; cls: string }> = {
  new: { label: '新增', cls: 'chipBrand' },
  continuing: { label: '持续', cls: 'chipInfo' },
  confirmed: { label: '已确认', cls: 'chipSuccess' },
  weakened: { label: '减弱', cls: 'chipWarning' },
  invalidated: { label: '失效', cls: 'chipDanger' },
  transformed: { label: '转化', cls: 'chipDefault' },
}

const FILTER_FAMILY_LABEL: Record<string, string> = {
  A: 'A 表面/质量偏差',
  B: 'B 状态/速度偏差',
  C: 'C 成交/参与偏差',
}

function statusChip(status: string) {
  const meta = SIGNAL_STATUS_META[status] ?? { label: status, cls: 'chipDefault' }
  return (
    <span className={`${styles.chip} ${styles[meta.cls]}`}>{meta.label}</span>
  )
}

/** 从结构化 evidencePayload 生成模板化解释（禁止大模型自由编写结论） */
function renderExplanation(signal: ReviewSignal): string {
  const ev = signal.evidencePayload
  const parts: string[] = []
  const trigger = signal.triggerPayload
  // 触发变量
  const triggerMetrics = trigger?.['metrics']
  if (Array.isArray(triggerMetrics) && triggerMetrics.length > 0) {
    const names = triggerMetrics
      .map((m) => (typeof m === 'object' && m !== null && 'name' in m ? String((m as { name: string }).name) : ''))
      .filter(Boolean)
    if (names.length > 0) {
      parts.push(`触发变量：${names.join('、')}`)
    }
  }
  // 历史分位
  const percentile = ev?.['historyPercentile120d']
  if (typeof percentile === 'number') {
    parts.push(`120日历史分位 ${percentile.toFixed(1)}`)
  }
  // 结构化偏差描述（如 "P保持高位 → Q下降"）
  const pattern = ev?.['pattern']
  if (typeof pattern === 'string' && pattern) {
    parts.push(pattern)
  }
  if (parts.length === 0) {
    return `信号类型 ${signal.signalType} 命中范围 ${signal.scopeName}`
  }
  return parts.join('；')
}

export interface SignalCardProps {
  signal: ReviewSignal
  active?: boolean
  onSelect?: (signal: ReviewSignal) => void
  onViewAttribution?: (signal: ReviewSignal) => void
  onViewHistory?: (signal: ReviewSignal) => void
  onAddTracking?: (signal: ReviewSignal) => void
}

export default function SignalCard({
  signal,
  active,
  onSelect,
  onViewAttribution,
  onViewHistory,
  onAddTracking,
}: SignalCardProps) {
  return (
    <div
      className={`${styles.signalCard} ${active ? styles.signalCardActive : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect?.(signal)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect?.(signal)
        }
      }}
    >
      <div className={styles.signalCardTop}>
        <div>
          <div className={styles.signalCardScope}>{signal.scopeName}</div>
          <div className={styles.signalCardType}>
            {FILTER_FAMILY_LABEL[signal.filterFamily] ?? signal.filterFamily}
            {' · '}
            {signal.signalType}
          </div>
        </div>
        {statusChip(signal.status)}
      </div>
      <div className={styles.signalCardMeta}>
        <span>首次：{signal.firstSeenDate}</span>
        <span>持续 {signal.durationDays} 日</span>
        {signal.coverageRatio !== null && (
          <span>coverage {(signal.coverageRatio * 100).toFixed(1)}%</span>
        )}
      </div>
      <div className={styles.signalCardExplain}>{renderExplanation(signal)}</div>
      <div className={styles.signalCardActions}>
        {onViewAttribution && (
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={(e) => {
              e.stopPropagation()
              onViewAttribution(signal)
            }}
          >
            查看归因
          </button>
        )}
        {onViewHistory && (
          <button
            type="button"
            className={styles.btn}
            onClick={(e) => {
              e.stopPropagation()
              onViewHistory(signal)
            }}
          >
            查看历史
          </button>
        )}
        {onAddTracking && (
          <button
            type="button"
            className={styles.btn}
            onClick={(e) => {
              e.stopPropagation()
              onAddTracking(signal)
            }}
          >
            加入追踪
          </button>
        )}
      </div>
    </div>
  )
}
