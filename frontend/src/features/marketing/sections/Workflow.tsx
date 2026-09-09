// Workflow（Full Alignment V1）：6 步 icon-grid + 底部 flow line。
// id=how-it-works（次级 CTA 锚定）。图标用内联 SVG（无 emoji、无图标依赖）。
import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { STEP_ICONS, type StepIconKey } from '../components/MarketingIcons'
import { WORKFLOW } from '../data/copy'
import styles from '../marketing.module.scss'

export default function Workflow() {
  return (
    <section
      className={clsx(styles.section, styles.sectionAlt)}
      id={WORKFLOW.id}
      data-testid="marketing-workflow"
    >
      <div className={styles.container}>
        <SectionHeading
          index={WORKFLOW.index}
          eyebrow={WORKFLOW.eyebrow}
          title={WORKFLOW.title}
          subtitle={WORKFLOW.subtitle}
        />
        <div className={styles.workflowGrid}>
          {WORKFLOW.steps.map((step, i) => {
            const Icon = STEP_ICONS[step.icon as StepIconKey]
            return (
              <ScrollReveal
                key={step.key}
                className={styles.revealFill}
                delay={i * 60}
              >
                <div className={styles.workflowCell}>
                  <span className={styles.workflowIcon}>
                    {Icon ? <Icon width={22} height={22} /> : null}
                  </span>
                  <div className={styles.workflowStepIndex}>
                    {String(i + 1).padStart(2, '0')}
                  </div>
                  <h3 className={styles.stepTitle}>{step.title}</h3>
                  <p className={styles.stepDesc}>{step.desc}</p>
                </div>
              </ScrollReveal>
            )
          })}
        </div>

        {/* 底部 flow line：全市场 → 候选 → 个股 → 自选 → 提醒 */}
        <div className={styles.workflowFlow} aria-hidden="true">
          {['全市场', '候选', '个股', '自选', '提醒'].map((node, j) => (
            <span key={node} className={styles.workflowFlowNode}>
              {j > 0 ? (
                <span className={styles.workflowFlowArrow}>→</span>
              ) : null}
              <span className={styles.workflowFlowLabel}>{node}</span>
            </span>
          ))}
        </div>
      </div>
    </section>
  )
}
