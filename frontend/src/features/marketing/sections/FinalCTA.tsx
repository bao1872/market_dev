// FinalCTA（Full Alignment V1 · 新增，位于 footer 之前）：
// 大 CTA section：标题 + 副标题 + 主/次按钮 + QQ 邀请码。
// 不使用普通 footer 直接结束。
import clsx from 'clsx'
import { FINAL_CTA } from '../data/copy'
import styles from '../marketing.module.scss'

export default function FinalCTA() {
  return (
    <section className={styles.finalCta} data-testid="marketing-final-cta">
      <div className={styles.container}>
        <div className={styles.finalCtaInner}>
          <h2 className={styles.finalCtaTitle}>{FINAL_CTA.title}</h2>
          <p className={styles.finalCtaSubtitle}>{FINAL_CTA.subtitle}</p>

          <div className={styles.ctaRow}>
            <a
              className={clsx(styles.btn, styles.btnPrimary, styles.finalCtaBtn)}
              href={FINAL_CTA.primaryCta.href}
            >
              {FINAL_CTA.primaryCta.label}
            </a>
            <a
              className={clsx(styles.btn, styles.btnGhost, styles.finalCtaBtn)}
              href={FINAL_CTA.secondaryCta.href}
              {...(FINAL_CTA.secondaryCta.external
                ? { target: '_blank', rel: 'noopener noreferrer' }
                : {})}
            >
              {FINAL_CTA.secondaryCta.label}
            </a>
          </div>

          {/* QQ 邀请码：复用已有截图，CSS 裁剪突出邀请码区域 */}
          <div className={styles.finalInvite}>
            <span className={styles.finalInviteTitle}>{FINAL_CTA.invite.title}</span>
            <div className={styles.finalInviteImgWrap}>
              <img
                className={styles.finalInviteImg}
                src={FINAL_CTA.invite.imageSrc}
                alt={FINAL_CTA.invite.imageAlt}
                loading="lazy"
              />
            </div>
            <span className={styles.finalInviteNote}>{FINAL_CTA.invite.note}</span>
          </div>
        </div>
      </div>
    </section>
  )
}
