// 小Z说事 → 盘迹（Full Alignment V1 · V1.2）：左侧真实雪球截图（非虚构文章卡），
// 右侧漏斗：板块 → 趋势上行 → 结构一致 → 成交未明显萎缩 → 高亮「值得研究」。
// 核心文案：不是替你选答案，而是把范围压缩。
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { XIAOZ } from '../data/copy'
import styles from '../marketing.module.scss'

export default function XiaozToPanji() {
  const maxCount = XIAOZ.funnel[0]?.count ?? 1

  return (
    <section
      className={`${styles.section} ${styles.sectionAlt}`}
      id="xiaoz"
      data-testid="marketing-xiaoz"
    >
      <div className={styles.container}>
        <SectionHeading
          index={XIAOZ.index}
          eyebrow={XIAOZ.eyebrow}
          title={XIAOZ.title}
          subtitle=""
        />
        <ScrollReveal>
          <div className={styles.xiaozGrid}>
            {/* 左：真实雪球截图 */}
            <figure
              className={styles.xiaozScreenshotFrame}
              data-testid="marketing-xiaoz-screenshot"
            >
              <img
                src={XIAOZ.imageSrc}
                alt={XIAOZ.imageAlt}
                loading="lazy"
              />
            </figure>

            {/* 右：漏斗 */}
            <div className={styles.xiaozFunnel}>
              <ul className={styles.labFunnelList}>
                {XIAOZ.funnel.map((row) => (
                  <li key={row.label} className={styles.xiaozFunnelRow}>
                    <span className={styles.labFunnelName}>{row.label}</span>
                    <span className={styles.labFunnelBarWrap}>
                      <span
                        className={styles.labFunnelBar}
                        style={{
                          width: `${Math.max(8, (row.count / maxCount) * 100)}%`,
                        }}
                      />
                    </span>
                    <span className={styles.labFunnelCount}>
                      {row.count}
                      {row.unit ?? ''}
                    </span>
                  </li>
                ))}
              </ul>
              <div className={styles.xiaozHighlight}>{XIAOZ.highlight}</div>
              <p className={styles.xiaozCore}>{XIAOZ.core}</p>
            </div>
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}
