// [MarketDashboard][PANJI-MARKET-OVERVIEW] - Layer 3 250 日市场轨迹 2×2 面板
//
// 硬合同：
//   - 4 张图 = 指数相对走势 / 涨跌家数 / 成交额 / 涨停跌停（顺序见 MARKET_HISTORY_CHARTS）。
//   - 全部只读 server 返回，前端不重算、不 slice；window 由 server 决定。
//   - null -> 断线 whitespace gap（由 MultiLineChart / buildLineData 保证，绝不补 0）。
import BreadthChart from './BreadthChart'
import { MARKET_HISTORY_CHARTS } from './marketOverviewConfig'
import type { BreadthPoint } from './types'
import styles from './dashboard.module.scss'

export default function MarketHistoryCharts({ points }: { points: BreadthPoint[] }) {
  if (points.length === 0) {
    return <div className={styles.stateBox}>当前时间范围暂无数据</div>
  }
  return (
    <div className={styles.historyGrid}>
      {MARKET_HISTORY_CHARTS.map((chart) => (
        <section key={chart.key} className={styles.chartCard}>
          <div className={styles.chartTitle}>{chart.title}</div>
          <BreadthChart
            points={points}
            height={260}
            series={chart.series.map((s) => ({
              field: s.field,
              label: s.label,
              color: s.color,
              scale: 'left',
              transform: s.transform,
            }))}
          />
        </section>
      ))}
    </div>
  )
}
