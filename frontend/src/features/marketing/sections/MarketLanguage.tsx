import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { FIRST_PYRAMID, MARKET_LANGUAGE } from '../data/copy'
import styles from '../marketing.module.scss'

type Props = {
  onOpenFieldDictionary: () => void
}

export default function MarketLanguage({ onOpenFieldDictionary }: Props) {
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
        {/* 第一金字塔渐进披露：低调入口，不在主导航、不占 section index */}
        <div className={styles.dictionaryLinkRow}>
          <button
            type="button"
            className={styles.dictionaryLink}
            onClick={onOpenFieldDictionary}
            data-testid="marketing-field-dictionary-trigger"
          >
            {FIRST_PYRAMID.drawer.triggerLabel}
            <span aria-hidden="true">→</span>
          </button>
        </div>
      </div>
    </section>
  )
}
