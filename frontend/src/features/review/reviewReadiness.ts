// [reviewReadiness] - Review 指标冷启动 / readiness 展示纯函数（无 SCSS/React 依赖）。
// C3：当 metric.value 为空但 raw_ready=true 时，必须以 rawValue 展示并标注历史不足，
// 不得显示 0 分、不得隐藏卡片、不得把 insufficient_history 写成计算失败。
// 本模块供 ScopeMetricsTable / EvidenceDrawer 等复用同一冷启动判定逻辑。
import type { ReviewMetricPayload } from './types'

/** 判断单指标是否处于冷启动状态并返回应展示的值。
 *
 * [PRD §7.1 Cold-Start + C3]：
 * - 冷启动 = status 为 insufficient_history，或 value 为空但 raw_ready=true
 *   （后端可能把"value 空但 raw_ready=true"标为 ready，此时必须回退 rawValue）；
 * - 冷启动时展示 rawValue，不得显示 0 分、不得隐藏卡片、不得写成计算失败。
 */
export function resolveMetricColdStart(payload: ReviewMetricPayload | null): {
  isCold: boolean
  displayValue: number | null
} {
  if (!payload) return { isCold: false, displayValue: null }
  const hasValue =
    payload.value !== null &&
    payload.value !== undefined &&
    !Number.isNaN(Number(payload.value))
  const rawReady = payload.readiness?.raw_ready === true
  const isCold = payload.status === 'insufficient_history' || (!hasValue && rawReady)
  return { isCold, displayValue: isCold ? payload.rawValue : payload.value }
}

/** 组装冷启动/历史不足的悬浮提示标题（供表格与抽屉复用）。 */
export function buildColdStartTitle(payload: ReviewMetricPayload): string {
  const parts: string[] = ['历史不足（冷启动）']
  if (payload.historyObservationCount !== null && payload.historyObservationCount !== undefined) {
    parts.push(`历史观测=${payload.historyObservationCount}条`)
  }
  const minRequired = payload.readiness?.min_required
  if (minRequired !== null && minRequired !== undefined) {
    parts.push(`需≥${minRequired}条`)
  }
  if (payload.readiness?.reason) {
    parts.push(payload.readiness.reason)
  }
  return parts.join(' · ')
}
