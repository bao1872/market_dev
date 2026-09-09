import clsx from 'clsx'
import GlowBackground from '../components/GlowBackground'
import { HERO } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Hero() {
  return (
    <section className={styles.hero} data-testid="marketing-hero">
      <GlowBackground />
      <div className={styles.container}>
        <div className={styles.heroInner}>
          <span className={styles.index}>{HERO.eyebrow}</span>
          <h1 className={styles.heroTitle}>{HERO.title}</h1>
          <p className={styles.heroSub}>{HERO.subtitle}</p>
          <div className={styles.ctaRow}>
            <a className={clsx(styles.btn, styles.btnPrimary)} href={HERO.primaryCta.href}>
              {HERO.primaryCta.label}
            </a>
            <a className={clsx(styles.btn, styles.btnGhost)} href={HERO.secondaryCta.href}>
              {HERO.secondaryCta.label}
            </a>
          </div>
        </div>
      </div>
    </section>
  )
}
