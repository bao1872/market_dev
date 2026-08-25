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
  /**
   * focusable：是否由 ReviewTerm 自身 label 担任键盘 focus owner。
   * - true（默认，standalone card/label）：label 自带 tabIndex=0 + aria-describedby，
   *   keyboard focus 展开 tooltip。
   * - false（嵌套在已有交互 trigger 内，如 role="tab" button）：label 不再创建
   *   第二个 tabIndex=0 stop，仅作展示；tooltip 由祖先 trigger 的 focus 驱动
   *   （调用方负责在祖先元素挂 aria-describedby + :focus-visible CSS）。
   */
  focusable?: boolean
  /**
   * tooltipId：外部传入的 tooltip 元素 id（用于嵌套场景，让祖先 trigger 的
   * aria-describedby 指向本 tooltip）。不传则由内部 useId() 生成。
   */
  tooltipId?: string
  className?: string
}

export default function ReviewTerm({
  termKey,
  label,
  help,
  compact = false,
  focusable = true,
  tooltipId: tooltipIdProp,
  className,
}: ReviewTermProps) {
  const term = termKey ? REVIEW_TERMS[termKey] : undefined
  const displayLabel = label ?? term?.label ?? ''
  const displayHelp = help ?? term?.help
  const [open, setOpen] = useState(false)
  const generatedTooltipId = useId()
  const tooltipId = tooltipIdProp ?? generatedTooltipId

  // 无 help 时只渲染纯 label（无 ⓘ、无 tooltip、无 aria 引用）
  if (!displayHelp) {
    return <span className={className}>{displayLabel}</span>
  }

  const rootClass = className ? `${styles.term} ${className}` : styles.term

  // 键盘 focus owner：仅当 focusable 时由 label 自身承担（避免嵌套 trigger 内出现
  // 第二个 tabIndex=0 stop，破坏 tablist 键盘模型）。
  const labelFocusProps = focusable
    ? {
        tabIndex: 0,
        onFocus: () => setOpen(true),
        onBlur: () => setOpen(false),
        'aria-describedby': tooltipId,
      }
    : {}

  return (
    <span
      className={rootClass}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span className={styles.termLabel} {...labelFocusProps}>
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
