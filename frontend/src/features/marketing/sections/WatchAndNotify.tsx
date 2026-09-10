// Watch + Notify（V1.6 · 重定位为盘中监控）：
// 左侧：一句 promise + 3 个用户场景（不是 5 条抽象流程） + 紧凑 flow；
// 右侧：真实飞书研究图 poster，视觉重量与左侧接近，不制造中间真空。
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { IconArrowRight } from '../components/MarketingIcons'
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
        <SectionHeading
          index={WATCH_NOTIFY.index}
          eyebrow={WATCH_NOTIFY.eyebrow}
          title={WATCH_NOTIFY.title}
          subtitle={WATCH_NOTIFY.subtitle}
        />
        <ScrollReveal>
          <div className={styles.watchStage}>
            {/* 左：promise + 3 个用户场景 + 紧凑 flow */}
            <div className={styles.watchScenarioSide}>
              <strong className={styles.watchPromise}>
                你不用一直盯着屏幕。
                <br />
                盘迹盯的是状态有没有变化。
              </strong>

              <div className={styles.watchScenarios}>
                {WATCH_NOTIFY.scenarios.map((item, index) => (
                  <article
                    key={item.title}
                    className={styles.watchScenario}
                  >
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    <div>
                      <h3>{item.title}</h3>
                      <p>{item.text}</p>
                    </div>
                  </article>
                ))}
              </div>

              <div className={styles.watchCompactFlow}>
                {WATCH_NOTIFY.flow.map((step, index) => (
                  <div key={step} className={styles.watchFlowNode}>
                    <span
                      className={
                        step === '盘中持续跟踪'
                          ? styles.watchFlowPrimary
                          : undefined
                      }
                    >
                      {step}
                    </span>
                    {index < WATCH_NOTIFY.flow.length - 1 && (
                      <IconArrowRight
                        width={14}
                        height={14}
                        aria-hidden="true"
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>

            {/* 右：真实飞书研究图 */}
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