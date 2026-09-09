// MarketLanguage（Full Alignment V1 视觉升级）：
// 不再六个同样的 card；改为「中心句 + 横向六维」排版。
// 第一金字塔仍是低调渐进披露入口（不在主导航、不占 section index）。
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

        {/* 中心句 */}
        <p className={styles.marketCenter}>{MARKET_LANGUAGE.centerSentence}</p>

        {/* 横向六维：大词 + 极短解释 */}
        <div className={clsx(styles.grid, styles.langRow)}>
          {MARKET_LANGUAGE.dimensions.map((dim, i) => (
            <ScrollReveal key={dim.key} className={styles.revealFill} delay={i * 50}>
              <div className={styles.langCell}>
                <h3 className={styles.langWord}>{dim.title}</h3>
                <p className={styles.langDesc}>{dim.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>

        {/* 第一金字塔渐进披露：低调入口 */}
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
