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

/** 判断单个聚合变量是否可展示（用于单元格级展示）。
 *
 * [PRD §7.1 Cold-Start 合同]：
 * - ready/partial：完整展示 value（normalized）+ delta + 历史分位
 * - insufficient_history：已有 rawValue，但历史 < 60 日 normalized 为 null。
 *   必须展示 rawValue + coverage + 历史不足原因，分位/delta 为空，不得显示"不可用"。
 * - unavailable：真不可用，显示缺省。
 */
export function isMetricDisplayable(payload: ReviewMetricPayload | null): boolean {
  if (!payload) return false
  return (
    payload.status === 'ready' ||
    payload.status === 'partial' ||
    payload.status === 'insufficient_history'
  )
}

/** 旧名称保留为向后兼容别名（TODO：全局统一后移除） */
export const isMetricAvailable = isMetricDisplayable

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
