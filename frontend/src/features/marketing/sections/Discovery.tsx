// Discovery（Full Alignment V1 + V1.4）：3 张机会入口卡，严格同构。
// V1.4：删除 story 卡的真实雪球截图，三卡统一为「编号 + 标题 + 三步 flow + 解释」，
//   一等高；截图只在 XiaozToPanji 作为低权重 evidence。
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { IconArrowRight } from '../components/MarketingIcons'
import { DISCOVERY } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Discovery() {
  return (
    <section
      className={styles.section}
      id="discovery"
      data-testid="marketing-discovery"
    >
      <div className={styles.container}>
        <SectionHeading
          index={DISCOVERY.index}
          eyebrow={DISCOVERY.eyebrow}
          title={DISCOVERY.title}
          subtitle={DISCOVERY.subtitle}
        />
        <div className={styles.discoveryGrid}>
          {DISCOVERY.entries.map((entry, i) => (
            <ScrollReveal
              key={entry.key}
              className={styles.revealFill}
              delay={i * 90}
            >
              <article className={styles.discoveryCard}>
                <div className={styles.discoveryCardTop}>
                  <span className={styles.discoveryCardIndex}>
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <h3 className={styles.discoveryCardTitle}>{entry.title}</h3>
                </div>

                {/* V1.4：三卡同构，一律三步 flow，不再有 media 分支 */}
                <ol className={styles.discoveryFlow}>
                  {entry.flow.map((step, j) => (
                    <li key={step} className={styles.discoveryFlowStep}>
                      {j > 0 ? (
                        <IconArrowRight
                          className={styles.discoveryFlowArrow}
                          width={16}
                          height={16}
                        />
                      ) : null}
                      <span className={styles.discoveryFlowNode}>{step}</span>
                    </li>
                  ))}
                </ol>

                <p className={styles.cardDesc}>{entry.desc}</p>
              </article>
            </ScrollReveal>
          ))}
        </div>
      </div>
    </section>
  )
}
