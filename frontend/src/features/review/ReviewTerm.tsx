// [ReviewTerm] - 描述: Review 统一展示术语组件（REVIEW-UX-CN-01）
//
// 职责：渲染 中文 label + 可选 hover/focus tooltip + aria。
// - 无业务逻辑、无数据请求；只消费 reviewCopy 的展示文案。
// - hover 文字或 ⓘ 图标都显示 tooltip；键盘 focus（tabIndex=0）也显示。
// - 不用浏览器原生 title=""：不支持换行、移动端无 hover、a11y 弱。
// - compact 模式隐藏 ⓘ 图标（卡片指标 / Tab 使用），仍保留 hover/focus tooltip。
// - tooltip 宽度约 240–320px，纯中文，最多 1–2 句。
import { useId, useState, type ReactNode } from 'react'
import { REVIEW_TERMS, type ReviewTermKey } from './reviewCopy'
import styles from './review.module.scss'

export interface ReviewTermProps {
  /** reviewCopy 中的术语 key（无显式 label/help 时使用） */
  termKey?: ReviewTermKey
  /** 显式 label 覆盖（无 termKey 时必须提供） */
  label?: ReactNode
  /** 显式 help 覆盖 */
  help?: string
  /** compact：隐藏 ⓘ 图标，仅 hover/focus label 显示 tooltip */
  compact?: boolean
  className?: string
}

export default function ReviewTerm({
  termKey,
  label,
  help,
  compact = false,
  className,
}: ReviewTermProps) {
  const term = termKey ? REVIEW_TERMS[termKey] : undefined
  const displayLabel = label ?? term?.label ?? ''
  const displayHelp = help ?? term?.help
  const [open, setOpen] = useState(false)
  const tooltipId = useId()

  // 无 help 时只渲染纯 label（无 ⓘ、无 tooltip、无 aria 引用）
  if (!displayHelp) {
    return <span className={className}>{displayLabel}</span>
  }

  const rootClass = className ? `${styles.term} ${className}` : styles.term

  return (
    <span
      className={rootClass}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span className={styles.termLabel} aria-describedby={tooltipId}>
        {displayLabel}
      </span>
      {!compact && (
        <span
          className={styles.termHelpIcon}
          role="button"
          tabIndex={0}
          aria-label={displayHelp}
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
        >
          ⓘ
        </span>
      )}
      <span
        className={open ? `${styles.termTooltip} ${styles.termTooltipOpen}` : styles.termTooltip}
        id={tooltipId}
        role="tooltip"
      >
        {displayHelp}
      </span>
    </span>
  )
}
