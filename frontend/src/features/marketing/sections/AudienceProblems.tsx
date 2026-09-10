// [V1.6] 第二屏（不编号）：从用户问题视角进入，在 Hero 之后、Discovery 之前。
// 不介绍产品功能堆栈，只回答「为什么需要它」。
import ScrollReveal from '../components/ScrollReveal'
import { AUDIENCE_PROBLEMS } from '../data/copy'
import styles from '../marketing.module.scss'

export default function AudienceProblems() {
  return (
    <section
      className={styles.audienceSection}
      data-testid="marketing-audience-problems"
    >
      <div className={styles.container}>
        <div className={styles.audienceHead}>
          <span className={styles.audienceEyebrow}>
            {AUDIENCE_PROBLEMS.eyebrow}
          </span>
          <h2>{AUDIENCE_PROBLEMS.title}</h2>
          <p>{AUDIENCE_PROBLEMS.subtitle}</p>
        </div>

        <ScrollReveal>
          <div className={styles.audienceGrid}>
            {AUDIENCE_PROBLEMS.problems.map((item, index) => (
              <article key={item.title} className={styles.audienceCard}>
                <span className={styles.audienceIndex}>
                  {String(index + 1).padStart(2, '0')}
                </span>
                <h3>{item.title}</h3>
                <p>{item.text}</p>
                <strong>{item.answer}</strong>
              </article>
            ))}
          </div>
        </ScrollReveal>

        <div className={styles.audienceFit}>
          <span>更适合</span>
          {AUDIENCE_PROBLEMS.fit.map((item) => (
            <em key={item}>{item}</em>
          ))}
        </div>
      </div>
    </section>
  )
}