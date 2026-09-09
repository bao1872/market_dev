// HeroScreener：营销 Hero 右侧静态股票筛选表。
// 数据来源：data/heroScreener.ts（确定性 mock，标注"演示数据，非实时行情"）。
// 视觉：6 行固定列（代码 / 名称 / 价格 / 涨跌幅），A 股惯例（涨红 / 跌绿）。
// 样式：marketing.module.scss 中 .heroScreener* 类。
import clsx from 'clsx'
import {
  HERO_SCREENER_NOTE,
  HERO_SCREENER_ROWS,
} from '../data/heroScreener'
import styles from '../marketing.module.scss'

function formatPrice(price: number): string {
  return price.toFixed(2)
}

function formatChangePct(pct: number): string {
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(2)}%`
}

export default function HeroScreener() {
  return (
    <div
      className={styles.heroScreener}
      data-testid="marketing-hero-screener"
    >
      <div className={styles.heroScreenerHead}>
        <span className={styles.heroScreenerTitle}>实时变化</span>
        <span className={styles.heroScreenerNote}>{HERO_SCREENER_NOTE}</span>
      </div>

      <div className={styles.heroScreenerTableWrap}>
          <table className={styles.heroScreenerTable}>
            <thead>
              <tr>
                <th scope="col">代码</th>
                <th scope="col">名称</th>
                <th scope="col" className={styles.heroScreenerRight}>
                  价格
                </th>
                <th scope="col" className={styles.heroScreenerRight}>
                  涨跌幅
                </th>
              </tr>
            </thead>
            <tbody>
              {HERO_SCREENER_ROWS.map((row) => (
                <tr key={row.code} data-direction={row.direction}>
                  <td className={styles.heroScreenerCode}>{row.code}</td>
                  <td>{row.name}</td>
                  <td
                    className={clsx(
                      styles.heroScreenerRight,
                      styles.heroScreenerPrice,
                    )}
                  >
                    {formatPrice(row.price)}
                  </td>
                  <td
                    className={clsx(
                      styles.heroScreenerRight,
                      row.direction === 'up'
                        ? styles.heroScreenerUp
                        : styles.heroScreenerDown,
                    )}
                  >
                    {formatChangePct(row.changePct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
    </div>
  )
}