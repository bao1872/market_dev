// Watch + Notify（Full Alignment V1 · 新增）：
// 标题 + 副标题 + 流程（发现候选 → 加入自选 → 状态变化 → 生成研究图片 → 飞书）。
// 右侧使用项目已有真实素材 poster_img1.webp（真实飞书截图更接近），置于 phone frame 内。
// 不自己画假的飞书通知卡。
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
          <div className={styles.watchGrid}>
            {/* 左：流程 */}
            <div className={styles.watchCopy}>
              <ol className={styles.watchSteps}>
                {WATCH_NOTIFY.steps.map((step, i) => (
                  <li key={step} className={styles.watchStep}>
                    <span className={styles.watchStepIndex}>
                      {String(i + 1).padStart(2, '0')}
                    </span>
                    <span className={styles.watchStepLabel}>{step}</span>
                    {i < WATCH_NOTIFY.steps.length - 1 ? (
                      <IconArrowRight
                        className={styles.watchStepArrow}
                        width={16}
                        height={16}
                        aria-hidden="true"
                      />
                    ) : null}
                  </li>
                ))}
              </ol>
              <p className={styles.watchNote}>{WATCH_NOTIFY.imageNote}</p>
            </div>

            {/* 右：phone frame 内放真实截图 */}
            <div className={styles.watchFrame}>
              <div className={styles.watchPhone}>
                <img
                  className={styles.watchImg}
                  src={WATCH_NOTIFY.imageSrc}
                  alt={WATCH_NOTIFY.imageAlt}
                  loading="lazy"
                />
              </div>
            </div>
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}
