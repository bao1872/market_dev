// [V1.6.1] Watch + Notify：盘中监控，1.08/0.92fr 左右栏，phone 顶部对齐 heading。
// 不再 import SectionHeading（核心定位：用户场景而非功能流程）。
import ScrollReveal from '../components/ScrollReveal'
import { WATCH_NOTIFY } from '../data/copy'
import styles from '../marketing.module.scss'

export default function WatchAndNotify() {
  return (
    <section
      className={styles.section}
      id="watch-notify"
      data-testid="marketing-watch-notify"
    >
      <div className={styles.container}>
        <ScrollReveal>
          <div className={styles.watchLayout}>
            <div className={styles.watchContent}>
              <span className={styles.watchHeadingEyebrow}>
                {WATCH_NOTIFY.eyebrow}
              </span>
              <h2 className={styles.watchHeading}>{WATCH_NOTIFY.title}</h2>
              <p className={styles.watchHeadingSubtitle}>
                {WATCH_NOTIFY.subtitle}
              </p>

              <strong className={styles.watchPromise}>
                你不用一直盯着屏幕。
                <br />
                盘迹盯的是状态有没有变化。
              </strong>

              <div className={styles.watchScenarios}>
                {WATCH_NOTIFY.scenarios.map((s) => (
                  <article key={s.title} className={styles.watchScenario}>
                    <span>{s.title}</span>
                    <p>{s.text}</p>
                  </article>
                ))}
              </div>

              <div className={styles.watchFlow}>
                {WATCH_NOTIFY.flow.map((step, i) => (
                  <span key={step}>
                    {step}
                    {i < WATCH_NOTIFY.flow.length - 1 ? ' → ' : ''}
                  </span>
                ))}
              </div>
            </div>

            <figure className={styles.watchVisual}>
              <div className={styles.watchPhone}>
                <img
                  src={WATCH_NOTIFY.imageSrc}
                  alt={WATCH_NOTIFY.imageAlt}
                  loading="lazy"
                />
              </div>
              <figcaption>{WATCH_NOTIFY.imageNote}</figcaption>
            </figure>
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}
