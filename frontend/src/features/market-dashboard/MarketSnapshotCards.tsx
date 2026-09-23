// [MarketDashboard][PANJI-MARKET-OVERVIEW] - Layer 1 今日市场快照 6 卡（顺序锁死）
//
// 硬合同：
//   - 6 卡顺序 = SNAPSHOT_CARD_ORDER（上证 / 深证 / 创业板 / 涨跌家数 / 成交额 / 涨停跌停）。
//   - 指数卡只显示点位 + 当日涨跌幅（红涨绿跌），**不塞交易所成交额**。
//   - 任一指标 unavailable -> 显示「—」（绝不显示 0 / 0% / 0 家）。
import type { MarketDashboardCard } from '@/api/marketDashboard'
import {
  deltaDirection,
  formatCount,
  formatDelta,
  formatIndex,
  formatTurnover,
} from './dashboardLogic'
import styles from './dashboard.module.scss'

function changeClass(value: number | null): string {
  const d = deltaDirection(value)
  if (d === 'up') return styles.up
  if (d === 'down') return styles.down
  return styles.flat
}

function IndexCard({ label, close, change }: { label: string; close: number | null; change: number | null }) {
  return (
    <div className={styles.card}>
      <div className={styles.cardLabel}>{label}</div>
      <div className={styles.snapshotPoint}>{formatIndex(close)}</div>
      <div className={`${styles.snapshotChange} ${changeClass(change)}`}>{formatDelta(change)}</div>
    </div>
  )
}

export default function MarketSnapshotCards({ cards }: { cards: MarketDashboardCard | undefined }) {
  if (!cards) return null
  const {
    sse_close,
    sse_change_pct,
    szse_close,
    szse_change_pct,
    chinext_close,
    chinext_change_pct,
    advance_count,
    decline_count,
    flat_count,
    turnover_amount,
    limit_up_count,
    limit_down_count,
  } = cards

  return (
    <div className={styles.snapshotGrid}>
      <IndexCard label="上证指数" close={sse_close} change={sse_change_pct} />
      <IndexCard label="深证成指" close={szse_close} change={szse_change_pct} />
      <IndexCard label="创业板指" close={chinext_close} change={chinext_change_pct} />

      <div className={styles.card}>
        <div className={styles.cardLabel}>涨跌家数</div>
        <div className={styles.snapshotCounts}>
          <span className={styles.up}>↑ {formatCount(advance_count)}</span>
          <span className={styles.down}>↓ {formatCount(decline_count)}</span>
          {flat_count !== null && flat_count !== undefined && (
            <span className={styles.cardHint}>平 {formatCount(flat_count)}</span>
          )}
        </div>
      </div>

      <div className={styles.card}>
        <div className={styles.cardLabel}>全市场成交额</div>
        <div className={styles.snapshotPoint}>{formatTurnover(turnover_amount)}</div>
      </div>

      <div className={styles.card}>
        <div className={styles.cardLabel}>涨停 / 跌停</div>
        <div className={styles.snapshotCounts}>
          <span className={styles.up}>涨停 {formatCount(limit_up_count)}</span>
          <span className={styles.down}>跌停 {formatCount(limit_down_count)}</span>
        </div>
      </div>
    </div>
  )
}
