// [V1.6.1] 第二屏：0.88/1.12fr 左右栏（intro + 4 rows），不再是居中 2×2 白卡 PPT。
import ScrollReveal from '../components/ScrollReveal'
import { AUDIENCE_PROBLEMS } from '../data/copy'
import styles from '../marketing.module.scss'

export default function AudienceProblems() {
  const a = AUDIENCE_PROBLEMS
  return (
    <section
      className={styles.audienceSection}
      data-testid="marketing-audience-problems"
    >
      <div className={styles.container}>
        <ScrollReveal>
          <div className={styles.audienceLayout}>
            {/* 左栏：标题 / subtitle / fit */}
            <div className={styles.audienceIntro}>
              <span className={styles.audienceEyebrow}>{a.eyebrow}</span>
              <h2>{a.title}</h2>
              <p>{a.subtitle}</p>
              <div className={styles.audienceFit}>
                <span>{a.intro.lead}</span>
                <em>{a.forWhom.join(' · ')}</em>
              </div>
            </div>

            {/* 右栏：4 条连续 row */}
            <ul className={styles.audienceProblemList}>
              {a.problems.map((p, i) => (
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

          <div className={styles.audienceNotFor}>
            <span>不适合</span>
            {a.notFor.map((t) => (
              <em key={t}>{t}</em>
            ))}
          </div>
        </ScrollReveal>
      </div>
    </section>
  )
}
