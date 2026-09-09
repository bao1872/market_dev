import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { MARKET_LANGUAGE } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketLanguage() {
  return (
    <section
      className={styles.section}
      id="market-language"
      data-testid="marketing-market-language"
    >
      <div className={styles.container}>
        <SectionHeading
          index={MARKET_LANGUAGE.index}
          eyebrow={MARKET_LANGUAGE.eyebrow}
          title={MARKET_LANGUAGE.title}
          subtitle={MARKET_LANGUAGE.subtitle}
        />
        <div className={clsx(styles.grid, styles.grid3)}>
          {MARKET_LANGUAGE.dimensions.map((dim, i) => (
            <ScrollReveal key={dim.key} className={styles.revealFill} delay={i * 60}>
              <div className={styles.dim}>
                <h3 className={styles.dimTitle}>{dim.title}</h3>
                <p className={styles.dimDesc}>{dim.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </div>
    </section>
  )
}
