import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { WORKFLOW } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Workflow() {
  return (
    <section
      className={clsx(styles.section, styles.sectionAlt)}
      id="workflow"
      data-testid="marketing-workflow"
    >
      <div className={styles.container}>
        <SectionHeading
          index={WORKFLOW.index}
          eyebrow={WORKFLOW.eyebrow}
          title={WORKFLOW.title}
          subtitle={WORKFLOW.subtitle}
        />
        <div className={styles.steps}>
          {WORKFLOW.steps.map((step, i) => (
            <ScrollReveal key={step.key} className={styles.revealFill} delay={i * 70}>
              <div className={styles.step}>
                <div className={styles.stepIndex}>{String(i + 1).padStart(2, '0')}</div>
                <h3 className={styles.stepTitle}>{step.title}</h3>
                <p className={styles.stepDesc}>{step.desc}</p>
              </div>
            </ScrollReveal>
          ))}
        </div>
      </div>
    </section>
  )
}
