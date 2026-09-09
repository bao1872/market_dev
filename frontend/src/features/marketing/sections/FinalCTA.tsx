// FinalCTA（Visual System V1.1 · 位于 footer 之前）：
// 大 CTA section：标题 + 副标题 + 主/次按钮 + 内容入口（无伪造二维码图）。
// V1.1 变更：移除伪造「QQ 邀请码」截图（qq_shot.png 为聊天 mockup，无真实 QR），
// invite 改为诚实文本入口，指向真实「小Z说事 · 雪球」。
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

          {/* 内容入口：诚实文本 CTA（无伪造二维码图） */}
          <div className={styles.finalInvite} data-testid="marketing-final-invite">
            <span className={styles.finalInviteTitle}>{FINAL_CTA.invite.title}</span>
            <a
              className={clsx(styles.btn, styles.btnGhost, styles.finalInviteCta)}
              href={FINAL_CTA.invite.href}
              {...(FINAL_CTA.invite.external
                ? { target: '_blank', rel: 'noopener noreferrer' }
                : {})}
            >
              {FINAL_CTA.invite.ctaLabel}
            </a>
            <span className={styles.finalInviteNote}>{FINAL_CTA.invite.note}</span>
          </div>
        </div>
      </div>
    </section>
  )
}
