// Hero（Full Alignment V1）：
// - 左：eyebrow + 标题 + 副标题 + 六维 proof chips + 主次 CTA + 诚实状态条
// - 右：HeroScreener 盘迹式筛选表（数据/逻辑见 components/HeroScreener）
// 已删除 Slice A 的未证实 claim（大字数字 / 实时同步 / 视频时长徽章）。
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
          {/* 左：文案 + proof + CTA + 状态条 */}
          <div className={styles.heroLeft}>
            <span className={styles.eyebrow}>{HERO.eyebrow}</span>
            <h1 className={styles.heroTitle}>{HERO.title}</h1>
            <p className={styles.heroSub}>{HERO.subtitle}</p>

            {/* 六维 proof：不编造数字，只描述盘迹看什么 */}
            <div className={styles.heroProof} aria-label={HERO.proof.label}>
              <span className={styles.heroProofLabel}>{HERO.proof.label}</span>
              <ul className={styles.heroProofDims}>
                {HERO.proof.dims.map((dim) => (
                  <li key={dim} className={styles.heroProofDim}>
                    {dim}
                  </li>
                ))}
              </ul>
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
                {HERO.secondaryCta.label}
                <span className={styles.btnArrow} aria-hidden="true">
                  ↓
                </span>
              </a>
            </div>
          </div>

          {/* 右：盘迹式筛选表 */}
          <div className={styles.heroRight} id="hero-screener">
            <HeroScreener />
          </div>
        </div>

        {/* 诚实状态条：演示数据 + 看状态不替判断（无实时同步假 claim） */}
        <ul
          className={styles.statusBadges}
          aria-label="状态说明"
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
