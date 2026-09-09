// Discovery（Full Alignment V1）：3 张机会入口卡。
// 每张卡：顶部编号 + 标题，中部 3 步 mini flow（带箭头），底部一句解释。
// 视觉节奏：与 Hero 两栏、Workflow 6 图标、Story 双栏交替，避免连续同尺寸卡片网格。
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

                {/* mini flow 可视化：3 步 + 箭头 */}
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
