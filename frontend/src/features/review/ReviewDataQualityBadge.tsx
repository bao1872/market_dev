// [ReviewDataQualityBadge] - 描述: 数据质量徽章（PRD §14.1、§17）
// 根据 scope/snapshot 状态显示 ready/partial/insufficient_history/unavailable
// 禁止伪造完成状态；coverage 不足或历史不足必须如实展示
import type { ReviewMetricPayload } from './types'
import styles from './review.module.scss'

export interface ReviewDataQualityBadgeProps {
  /** scope/snapshot 状态：ready/insufficient_history/partial/unavailable */
  status: string
  /** 可选 coverage（0-1），低于门禁显示警告色 */
  coverage?: number | null
}

const STATUS_META: Record<string, { label: string; cls: string }> = {
  ready: { label: '就绪', cls: 'chipSuccess' },
  partial: { label: '部分', cls: 'chipWarning' },
  insufficient_history: { label: '历史不足', cls: 'chipWarning' },
  unavailable: { label: '不可用', cls: 'chipDanger' },
  pending: { label: '待计算', cls: 'chipDefault' },
  running: { label: '计算中', cls: 'chipInfo' },
  failed: { label: '失败', cls: 'chipDanger' },
}

/** 判断单个聚合变量是否可用（用于单元格级展示） */
export function isMetricAvailable(payload: ReviewMetricPayload | null): boolean {
  if (!payload) return false
  return payload.status === 'ready' || payload.status === 'partial'
}

export default function ReviewDataQualityBadge({
  status,
  coverage,
}: ReviewDataQualityBadgeProps) {
  const meta = STATUS_META[status] ?? { label: status || '未知', cls: 'chipDefault' }
  // coverage 低于 0.95 门禁时降级为警告（PRD §11.1）
  let cls = meta.cls
  if (coverage !== undefined && coverage !== null && coverage < 0.95 && cls === 'chipSuccess') {
    cls = 'chipWarning'
  }
  return (
    <span className={`${styles.chip} ${styles[cls]}`}>{meta.label}</span>
  )
}
