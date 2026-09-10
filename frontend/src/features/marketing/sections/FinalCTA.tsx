// FinalCTA（Visual System V1.1 + V1.5 · 位于 footer 之前）：
// 大 CTA section：标题 + 副标题 + 主/次按钮。
// [V1.5] 删除独立 invite 入口（社区出口已收束进 Footer 双二维码）；
// 次级 CTA 改为「加入交流 → #community」。只做产品转化 + 社区轻入口。
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
            >
              {FINAL_CTA.secondaryCta.label}
            </a>
          </div>
        </div>
      </div>
    </section>
  )
}
