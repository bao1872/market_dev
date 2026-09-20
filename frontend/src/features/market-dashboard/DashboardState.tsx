// [MarketDashboard] - feature-local 状态组件（loading / forbidden / not-found / error / empty / retry）
// 不抽全项目通用 StateBox；review 等处保持原样，避免扩大影响面。
import styles from './dashboard.module.scss'

export type DashboardStateKind = 'loading' | 'forbidden' | 'not-found' | 'error' | 'empty'

export interface DashboardStateProps {
  kind: DashboardStateKind
  title?: string
  desc?: string
  onRetry?: () => void
}

export default function DashboardState({ kind, title, desc, onRetry }: DashboardStateProps) {
  const heading =
    title ??
    (kind === 'loading'
      ? '加载中…'
      : kind === 'forbidden'
        ? '没有访问权限'
        : kind === 'empty'
          ? '暂无数据'
          : '加载失败')

  const showRetry = !!onRetry && kind !== 'empty' && kind !== 'forbidden' && kind !== 'loading'

  return (
    <div className={styles.stateBox} data-kind={kind}>
      <div className={styles.stateTitle}>{heading}</div>
      {desc && <div className={styles.stateDesc}>{desc}</div>}
      {showRetry && (
        <button type="button" className={styles.btn} onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  )
}
