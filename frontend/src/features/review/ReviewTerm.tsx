// [ReviewTerm] - 描述: Review 统一展示术语组件（REVIEW-UX-CN-01 / REVIEW-UX-CLOSURE-02 P0-6）
//
// 职责：渲染 中文 label + 可选 hover/focus tooltip + aria。
// - 无业务逻辑、无数据请求；只消费 reviewCopy 的展示文案。
// - hover 文字或 ⓘ 图标都显示 tooltip；键盘 focus（tabIndex=0）也显示。
// - 不用浏览器原生 title=""：不支持换行、移动端无 hover、a11y 弱。
// - compact 模式隐藏 ⓘ 图标（卡片指标 / Tab 使用），仍保留 hover/focus tooltip。
// - tooltip 宽度约 240–360px，纯中文，最多 1–2 句。
// - P0-6：tooltip 经 React Portal 渲染到 document.body，position: fixed 固定定位，
//   高层 z-index，不成为任何 overflow:auto/hidden 滚动容器的 child → 不被裁切。
//   支持碰撞检测：右侧空间不足→向左展开；底部不足→向上偏移；顶部不足→向下偏移。
//   支持 mouse hover / keyboard focus（aria-describedby）/ 移动端 tap 切换。
import { useId, useLayoutEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
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
  /** focusable：是否由 ReviewTerm 自身 label 担任键盘 focus owner。 */
  focusable?: boolean
  /** tooltipId：外部传入的 tooltip 元素 id（用于嵌套场景）。 */
  tooltipId?: string
  className?: string
}

const TOOLTIP_MAX_WIDTH = 340
const TOOLTIP_MARGIN = 8

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
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null)
  const labelRef = useState<HTMLElement | null>(null)
  const generatedTooltipId = useId()
  const tooltipId = tooltipIdProp ?? generatedTooltipId

  // 计算 tooltip 固定定位坐标（碰撞检测 + flip）
  useLayoutEffect(() => {
    if (!open || !labelRef[0]) {
      setCoords(null)
      return
    }
    const anchor = labelRef[0].getBoundingClientRect()
    const tooltipH = 120 // 估算高度，布局后再校正
    const tooltipW = TOOLTIP_MAX_WIDTH
    const vw = window.innerWidth
    const vh = window.innerHeight

    // 默认：锚点下方、左对齐
    let left = anchor.left
    let top = anchor.bottom + TOOLTIP_MARGIN

    // 右侧空间不足 → 向左展开（右边缘对齐锚点右边缘）
    if (left + tooltipW > vw - TOOLTIP_MARGIN) {
      left = Math.max(TOOLTIP_MARGIN, anchor.right - tooltipW)
    }
    // 底部空间不足 → 向上偏移（显示在锚点上方）
    if (top + tooltipH > vh - TOOLTIP_MARGIN) {
      top = Math.max(TOOLTIP_MARGIN, anchor.top - tooltipH - TOOLTIP_MARGIN)
    }
    setCoords({ top, left })
  }, [open, labelRef])

  // 无 help 时只渲染纯 label（无 ⓘ、无 tooltip、无 aria 引用）
  if (!displayHelp) {
    return <span className={className}>{displayLabel}</span>
  }

  const rootClass = className ? `${styles.term} ${className}` : styles.term

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
      onClick={() => setOpen((v) => !v)} // 移动端 tap 切换
    >
      <span
        ref={(el) => {
          labelRef[1](el)
        }}
        className={styles.termLabel}
        {...labelFocusProps}
      >
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
      {open &&
        coords &&
        createPortal(
          <span
            className={styles.termTooltip}
            id={tooltipId}
            role="tooltip"
            style={{
              position: 'fixed',
              top: coords.top,
              left: coords.left,
              maxWidth: TOOLTIP_MAX_WIDTH,
              width: 'max-content',
            }}
          >
            {displayHelp}
          </span>,
          document.body,
        )}
    </span>
  )
}
