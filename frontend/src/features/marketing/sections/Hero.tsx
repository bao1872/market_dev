// Hero（Full Alignment V1 + V1.2 真实产品大屏 + V1.4 background 化）：
// - 上方：eyebrow + 标题 + 副标题 + 六维 proof chips + 主次 CTA + 诚实状态条
// - V1.4：产品视觉降为 Hero 的绝对定位背景层（右侧大背景），文字压在它上面；
//   移动端（<=900）恢复为「文字 ↓ 产品图」的正常文档流。
// 数据来源：data/copy.ts HERO + MARKETING_MEDIA；样式在 marketing.module.scss。
import clsx from 'clsx'
import GlowBackground from '../components/GlowBackground'
import ProductDeviceStage from '../components/ProductDeviceStage'
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

      {/* V1.4：产品视觉作为背景层，不占正常文档流（aria-hidden） */}
      <div className={styles.heroProductBackdrop} aria-hidden="true">
        <ProductDeviceStage mode="backdrop" />
      </div>

      <div className={styles.container}>
        <div className={styles.heroIntro}>
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

          {/* 诚实状态条：真实产品界面 + 历史示例仅用于功能说明 */}
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
      </div>
    </section>
  )
}