// [MarketDashboard][R3B] - 大盘页 ranking summary block（行业 / 概念 各自 Top5 + Bottom5）
//
// 规则：
//   - 数据只用现有 rankings API（server 已排序，前端**不重排**）；
//   - 不使用 Explorer collection API 重复实现 ranking；
//   - 每个 block 独立 loading / error / empty —— 单个 ranking 失败**不得**影响主图；
//   - 空 ranking 如实呈现「暂无」，**不解释成 0**；
//   - delta 的红涨绿跌是「变化方向」语义，此处允许使用（与 series identity 无关）。
import { Link } from 'react-router-dom'
import DashboardState, { type DashboardStateKind } from './DashboardState'
import { deltaDirection, formatDelta } from './dashboardLogic'
import type { MarketRankingSummaryConfig } from './marketOverviewConfig'
import type { RankingItem } from './types'
import styles from './dashboard.module.scss'

const DIR_CLASS: Record<'up' | 'down' | 'flat', string> = {
  up: styles.up,
  down: styles.down,
  flat: styles.flat,
}

export interface MarketRankingSummaryProps {
  config: MarketRankingSummaryConfig
  top: RankingItem[]
  bottom: RankingItem[]
  /** null = 正常渲染（与 DashboardState 语义一致）。 */
  state: DashboardStateKind | null
  errorDetail?: string | null
  onRetry: () => void
}

export default function MarketRankingSummary({
  config,
  top,
  bottom,
  state,
  errorDetail,
  onRetry,
}: MarketRankingSummaryProps) {
  const empty = !state && top.length === 0 && bottom.length === 0

  return (
    <section className={styles.summaryBlock} data-summary={config.key}>
      <div className={styles.summaryHead}>
        <span className={styles.summaryTitle}>{config.title}</span>
        <Link className={styles.summaryLink} to={config.seeAllLink}>
          查看全部{config.title}
        </Link>
      </div>

      {state && (
        <DashboardState
          kind={state}
          desc={
            state === 'forbidden'
              ? '你当前没有市场数据访问权限'
              : state === 'not-found'
                ? '板块排行数据尚未生成'
                : errorDetail || '板块排行加载失败'
          }
          onRetry={onRetry}
        />
      )}
      {empty && <DashboardState kind="empty" desc="暂无可展示的板块排行" />}

      {!state && !empty && (
        <>
          <RankingRowList label="Top 5" items={top} explorerLink={config.explorerLink} />
          <RankingRowList label="Bottom 5" items={bottom} explorerLink={config.explorerLink} />
        </>
      )}
    </section>
  )
}

function RankingRowList({
  label,
  items,
  explorerLink,
}: {
  label: string
  items: RankingItem[]
  explorerLink: (boardId: string) => string
}) {
  return (
    <div className={styles.summaryList}>
      <div className={styles.summaryListTitle}>{label}</div>
      {items.length === 0 && <div className={styles.summaryEmpty}>暂无</div>}
      {items.map((item) => (
        <Link key={item.board_id} className={styles.summaryRow} to={explorerLink(item.board_id)}>
          <span className={styles.summaryName}>{item.board_name}</span>
          <span className={styles.summaryMetrics}>
            <span className={styles.summaryMetricLabel}>MA5</span>
            <span className={DIR_CLASS[deltaDirection(item.delta.ma5)]}>{formatDelta(item.delta.ma5)}</span>
            <span className={styles.summaryMetricLabel}>MA10</span>
            <span className={DIR_CLASS[deltaDirection(item.delta.ma10)]}>{formatDelta(item.delta.ma10)}</span>
          </span>
        </Link>
      ))}
    </div>
  )
}
