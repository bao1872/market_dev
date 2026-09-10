// [V1.6.2] 第二屏：exact owner copy 消费 —— 只访问 copy.ts 中存在的字段
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
        <ScrollReveal>
          <div className={styles.audienceLayout}>
            {/* 左栏：标题 + subtitle + fit */}
            <div className={styles.audienceIntro}>
              <span className={styles.audienceEyebrow}>
                {AUDIENCE_PROBLEMS.eyebrow}
              </span>
              <h2>{AUDIENCE_PROBLEMS.title}</h2>
              <p>{AUDIENCE_PROBLEMS.subtitle}</p>
              <div className={styles.audienceFit}>
                <span>更适合</span>
                {AUDIENCE_PROBLEMS.fit.map((item) => (
                  <em key={item}>{item}</em>
                ))}
              </div>
            </div>

            {/* 右栏：4 条连续 row */}
            <ul className={styles.audienceProblemList}>
              {AUDIENCE_PROBLEMS.problems.map((p, i) => (
                <li key={p.title} className={styles.audienceProblemRow}>
                  <span className={styles.audienceProblemNo}>
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <div className={styles.audienceProblemBody}>
                    <h3>{p.title}</h3>
                    <p>{p.text}</p>
                  </div>
                  <strong className={styles.audienceProblemAnswer}>
                    {p.answer}
                  </strong>
                </li>
              ))}
            </ul>
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}
