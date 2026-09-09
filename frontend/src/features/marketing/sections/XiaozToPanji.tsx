// 小Z说事 → 盘迹（Full Alignment V1 · V1.2 · V1.4）。
// V1.4 产品叙事改为三段路径：① 小Z说事发现方向 → ② 进入盘迹用自己的选股审美筛 → ③ 得到候选。
// 雪球截图只作为 01 卡的低权重 evidence（小窗），不再是主视觉；preferences 仅为交互示例，不声明固定策略。
// 数据来自 data/copy.ts XIAOZ，组件只消费不重定义。
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { XIAOZ } from '../data/copy'
import styles from '../marketing.module.scss'

export default function XiaozToPanji() {
  return (
    <section
      className={`${styles.section} ${styles.sectionAlt}`}
      id="xiaoz"
      data-testid="marketing-xiaoz"
    >
      <div className={styles.container}>
        <SectionHeading
          index={XIAOZ.index}
          eyebrow={XIAOZ.eyebrow}
          title={XIAOZ.title}
          subtitle={XIAOZ.subtitle}
        />
        <ScrollReveal>
          <div className={styles.xiaozJourney}>
            {/* 01 — 内容发现（雪球截图降级为 evidence 小窗） */}
            <article className={styles.xiaozJourneyStep}>
              <span className={styles.xiaozStepIndex}>{XIAOZ.steps[0].index}</span>
              <span className={styles.xiaozStepEyebrow}>
                {XIAOZ.steps[0].eyebrow}
              </span>
              <h3 className={styles.xiaozStepTitle}>{XIAOZ.steps[0].title}</h3>
              <p className={styles.xiaozStepText}>{XIAOZ.steps[0].text}</p>

              <figure className={styles.xiaozEvidence}>
                <img src={XIAOZ.imageSrc} alt={XIAOZ.imageAlt} loading="lazy" />
              </figure>

              <div className={styles.xiaozSignal}>
                示例：{XIAOZ.sourceExample.direction} {XIAOZ.sourceExample.note}
              </div>
            </article>

            <span className={styles.xiaozJourneyArrow} aria-hidden="true">
              →
            </span>

            {/* 02 — 进入盘迹：用户自己的选股审美（视觉主角） */}
            <article
              className={`${styles.xiaozJourneyStep} ${styles.xiaozJourneyPrimary}`}
            >
              <span className={styles.xiaozStepIndex}>{XIAOZ.steps[1].index}</span>
              <span className={styles.xiaozStepEyebrow}>
                {XIAOZ.steps[1].eyebrow}
              </span>
              <h3 className={styles.xiaozStepTitle}>{XIAOZ.steps[1].title}</h3>
              <p className={styles.xiaozStepText}>{XIAOZ.steps[1].text}</p>

              <div className={styles.xiaozPreferenceGrid}>
                {XIAOZ.preferences.map((pref) => (
                  <span
                    key={pref.label}
                    className={
                      pref.active ? styles.preferenceActive : styles.preference
                    }
                    data-testid="marketing-xiaoz-preference"
                  >
                    {pref.label}
                  </span>
                ))}
              </div>
            </article>

            <span className={styles.xiaozJourneyArrow} aria-hidden="true">
              →
            </span>

            {/* 03 — 候选范围 */}
            <article className={styles.xiaozJourneyStep}>
              <span className={styles.xiaozStepIndex}>{XIAOZ.steps[2].index}</span>
              <span className={styles.xiaozStepEyebrow}>
                {XIAOZ.steps[2].eyebrow}
              </span>
              <h3 className={styles.xiaozStepTitle}>{XIAOZ.steps[2].title}</h3>

              <div className={styles.xiaozResult}>
                <div className={styles.xiaozResultColumn}>
                  <strong className={styles.xiaozResultNum}>
                    {XIAOZ.resultExample.before}
                  </strong>
                  <span>{XIAOZ.resultExample.beforeLabel}</span>
                </div>
                <span className={styles.xiaozResultArrow} aria-hidden="true">
                  →
                </span>
                <div className={styles.xiaozResultColumn}>
                  <strong className={styles.xiaozResultNum}>
                    {XIAOZ.resultExample.after}
                  </strong>
                  <span>{XIAOZ.resultExample.afterLabel}</span>
                </div>
              </div>

              <p className={styles.xiaozResultNote}>{XIAOZ.steps[2].text}</p>
            </article>
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}