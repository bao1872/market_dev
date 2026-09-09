// HeroScreener：营销 Hero 右侧盘迹式筛选表。
// 与行情软件的区别在于列：不是代码/价格/涨跌幅，而是
//   股票 / 趋势 / 结构 / 动量 / 量能 / 筹码 / 最近变化
// 让访客一眼看出"盘迹看的是状态，不是涨跌数字"。
// 数据来源：data/heroScreener.ts（确定性 mock，标注"演示数据，非实时行情"）。
import clsx from 'clsx'
import {
  HERO_SCREENER_NOTE,
  HERO_SCREENER_ROWS,
} from '../data/heroScreener'
import styles from '../marketing.module.scss'

export default function HeroScreener() {
  return (
    <div className={styles.heroScreener} data-testid="marketing-hero-screener">
      {/* 顶部 filter shell：搜索框 + 一行条件 chips，模仿真实 MarketWorkspace */}
      <div className={styles.heroScreenerShell}>
        <div className={styles.heroScreenerSearch} aria-hidden="true">
          <span className={styles.heroScreenerSearchDot} />
          筛选 · 全市场
        </div>
        <div className={styles.heroScreenerChips}>
          <span className={clsx(styles.heroScreenerChip, styles.heroScreenerChipActive)}>
            趋势 = 上行
          </span>
          <span className={styles.heroScreenerChip}>结构 = 偏强</span>
          <span className={styles.heroScreenerChip}>动量 &gt; 0</span>
        </div>
      </div>

      <div className={styles.heroScreenerHead}>
        <span className={styles.heroScreenerTitle}>候选状态</span>
        <span className={styles.heroScreenerNote}>{HERO_SCREENER_NOTE}</span>
      </div>

      <div className={styles.heroScreenerTableWrap}>
        <table className={styles.heroScreenerTable}>
          <thead>
            <tr>
              <th scope="col">股票</th>
              <th scope="col">趋势</th>
              <th scope="col">结构</th>
              <th scope="col">动量</th>
              <th scope="col">量能</th>
              <th scope="col">筹码</th>
              <th scope="col" className={styles.heroScreenerRight}>
                最近变化
              </th>
            </tr>
          </thead>
          <tbody>
            {HERO_SCREENER_ROWS.map((row) => (
              <tr key={row.name} data-direction={row.dir}>
                <td className={styles.heroScreenerName}>{row.name}</td>
                <td
                  className={clsx(
                    styles.heroScreenerCell,
                    row.trend === '上行'
                      ? styles.heroScreenerUp
                      : row.trend === '下行'
                        ? styles.heroScreenerDown
                        : styles.heroScreenerNeutral,
                  )}
                >
                  {row.trend}
                </td>
                <td className={styles.heroScreenerCell}>{row.structure}</td>
                <td className={styles.heroScreenerCell}>{row.momentum}</td>
                <td className={styles.heroScreenerCell}>{row.volume}</td>
                <td className={styles.heroScreenerCell}>{row.chip}</td>
                <td
                  className={clsx(
                    styles.heroScreenerRight,
                    styles.heroScreenerRecent,
                    row.recentChange ? styles.heroScreenerRecentOn : styles.heroScreenerRecentOff,
                  )}
                >
                  {row.recentChange ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
