// [ReviewTerm] - 描述: Review 统一展示术语组件（REVIEW-UX-CN-01 / REVIEW-UX-CLOSURE-02 P0-6 / CORRECTION-01 P0-A/P0-B）
//
// 职责：渲染 中文 label + 可选 hover/focus tooltip + aria。
// - 无业务逻辑、无数据请求；只消费 reviewCopy 的展示文案。
// - hover 文字或 ⓘ 图标都显示 tooltip；键盘 focus（tabIndex=0）也显示。
// - 不用浏览器原生 title=""：不支持换行、移动端无 hover、a11y 弱。
// - compact 模式隐藏 ⓘ 图标（卡片指标 / Tab 使用），仍保留 hover/focus tooltip。
// - tooltip 宽度约 240–340px，纯中文，最多 1–2 句。
// - P0-6：tooltip 经 React Portal 渲染到 document.body，position: fixed 固定定位，
//   高层 z-index，不成为任何 overflow:auto/hidden 滚动容器的 child → 不被裁切。
// - P0-B：碰撞定位使用 tooltip **真实渲染尺寸**（getBoundingClientRect），
//   不猜测固定高度；右侧不足→向左、底部不足→向上、顶部不足→clamp。
//   resize / scroll 时若仍 open 则重新定位（监听 window resize；scroll 关闭后由 hover/focus 重开）。
// - P0-A：anchor 用真正的 useRef（非 useState），不把 state tuple 放进 effect dependency，
//   避免 repeated render loop / maximum update depth。
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { REVIEW_TERMS, type ReviewTermKey } from './reviewCopy'
import { computeTooltipPosition, DEFAULT_TOOLTIP_MARGIN } from './tooltipGeometry'
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
  // P0-A：真正的 DOM ref，非 useState。绝不放入 effect dependency。
  const anchorRef = useRef<HTMLElement | null>(null)
  const tooltipRef = useRef<HTMLSpanElement | null>(null)
  const generatedTooltipId = useId()
  const tooltipId = tooltipIdProp ?? generatedTooltipId

  // P0-B：open 时，先渲染 tooltip（visibility:hidden），量取真实尺寸，再算位置。
  useLayoutEffect(() => {
    if (!open) {
      setCoords(null)
      return
    }
    const anchorEl = anchorRef.current
    const tooltipEl = tooltipRef.current
    if (!anchorEl || !tooltipEl) return
    const anchorRect = anchorEl.getBoundingClientRect()
    const tooltipRect = tooltipEl.getBoundingClientRect()
    const next = computeTooltipPosition(
      anchorRect,
      tooltipRect,
      { width: window.innerWidth, height: window.innerHeight },
      DEFAULT_TOOLTIP_MARGIN,
    )
    setCoords(next)
  }, [open])

  // resize 时若仍打开则重新定位（scroll 同理：监听 window scroll，关闭后由 hover/focus 重开）。
  useEffect(() => {
    if (!open) return
    const reposition = () => {
      const anchorEl = anchorRef.current
      const tooltipEl = tooltipRef.current
      if (!anchorEl || !tooltipEl) return
      const anchorRect = anchorEl.getBoundingClientRect()
      const tooltipRect = tooltipEl.getBoundingClientRect()
      setCoords(
        computeTooltipPosition(
          anchorRect,
          tooltipRect,
          { width: window.innerWidth, height: window.innerHeight },
          DEFAULT_TOOLTIP_MARGIN,
        ),
      )
    }
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open])

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
          anchorRef.current = el
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
        createPortal(
          <span
            ref={(el) => {
              tooltipRef.current = el
            }}
            className={styles.termTooltip}
            id={tooltipId}
            role="tooltip"
            style={{
              position: 'fixed',
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              maxWidth: TOOLTIP_MAX_WIDTH,
              width: 'max-content',
              // coords 计算前隐藏，避免未定位瞬间闪现于左上角
              visibility: coords ? 'visible' : 'hidden',
            }}
          >
            {displayHelp}
          </span>,
          document.body,
        )}
    </span>
  )
}
