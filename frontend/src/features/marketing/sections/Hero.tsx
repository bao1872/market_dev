// Hero（参考图 #2 对齐）：
// - 左侧：标题 + 副标题 + "1000+ 行业图" 大字 stat + 主次按钮；
// - 右侧：HeroScreener 静态股票筛选表（data/heroScreener.ts）；
// - 底部：statusBadges 状态条（多市场同步 / 盘中持续刷新）。
// 数据来源：data/copy.ts HERO + data/heroScreener.ts；样式在 marketing.module.scss。
import clsx from 'clsx'
import GlowBackground from '../components/GlowBackground'
import HeroScreener from '../components/HeroScreener'
import { HERO } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Hero() {
  return (
    <section
      className={styles.hero}
      data-testid="marketing-hero"
      id="hero"
    >
      <GlowBackground />
      <div className={styles.container}>
        <div className={styles.heroGrid}>
          {/* 左：标题 + 副标题 + stat + CTA */}
          <div className={styles.heroLeft}>
            <span className={styles.index}>{HERO.eyebrow}</span>
            <h1 className={styles.heroTitle}>{HERO.title}</h1>
            <p className={styles.heroSub}>{HERO.subtitle}</p>

            <div className={styles.heroStat} data-testid="marketing-hero-stat">
              <span className={styles.heroStatValue}>
                {HERO.stat.value}
              </span>
              <span className={styles.heroStatLabel}>
                {HERO.stat.label}
              </span>
            </div>

            <div className={styles.ctaRow}>
              <a
                className={clsx(styles.btn, styles.btnPrimary)}
                href={HERO.primaryCta.href}
                data-testid="marketing-hero-cta-primary"
              >
                {HERO.primaryCta.label}
              </a>
              <a
                className={clsx(styles.btn, styles.btnGhost)}
                href={HERO.secondaryCta.href}
                data-testid="marketing-hero-cta-secondary"
              >
                <span>{HERO.secondaryCta.label}</span>
                <span
                  className={styles.btnDuration}
                  aria-label={`演示时长 ${HERO.secondaryCta.duration}`}
                >
                  {HERO.secondaryCta.duration}
                </span>
              </a>
            </div>
          </div>

          {/* 右：静态股票筛选表 */}
          <div className={styles.heroRight} id="hero-screener">
            <HeroScreener />
          </div>
        </div>

        {/* 底部：status badges */}
        <ul
          className={styles.statusBadges}
          aria-label="实时状态"
          data-testid="marketing-hero-status"
        >
          {HERO.statusBadges.map((badge) => (
            <li
              key={badge.text}
              className={clsx(
                styles.statusBadge,
                badge.dot === 'green'
                  ? styles.statusBadgeGreen
                  : styles.statusBadgeBlue,
              )}
            >
              <span className={styles.statusBadgeDot} aria-hidden="true" />
              <span>{badge.text}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}