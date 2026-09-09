import styles from '../marketing.module.scss'

interface SectionHeadingProps {
  index: string
  eyebrow: string
  title: string
  subtitle: string
}

// 每屏统一标题结构：序号 + eyebrow + 主标题 + 副标题
export default function SectionHeading({ index, eyebrow, title, subtitle }: SectionHeadingProps) {
  return (
    <div className={styles.sectionHead}>
      <span className={styles.index}>{index}</span>
      <div className={styles.eyebrow}>{eyebrow}</div>
      <h2 className={styles.title}>{title}</h2>
      <p className={styles.subtitle}>{subtitle}</p>
    </div>
  )
}
