// [AtomicFactsDrawer] - 描述: /stock/:symbol 状态观察右侧 overlay 抽屉
// 点击「显示状态观察」后打开；宽度 min(1080px, calc(100vw - 48px))，固定 overlay 不压缩主 K 线。
// 宽屏 4 列 / 普通桌面 2 列 / 小屏 1 列（由内部 grid 响应式）。
// 下方全宽近期变化 + 「更多观察」默认收起按分组渲染 Aux（T3/T6/V1 永不出现）。
// 焦点管理：打开后聚焦关闭按钮、焦点 trap（Tab/Shift+Tab 限制在抽屉内）、
//   关闭后恢复打开前焦点、body 滚动锁定。
// 关闭方式：Escape / 点击遮罩 / 关闭按钮；role=dialog aria-modal 完整。
import { useCallback, useEffect, useRef } from 'react'
import { AtomicFactsPanel } from './AtomicFactsPanel'
import styles from './AtomicFactsPanel.module.scss'

interface AtomicFactsDrawerProps {
  symbol: string | undefined
  open: boolean
  onClose: () => void
}

// 可聚焦元素选择器（用于焦点 trap）
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'textarea',
  'input',
  'select',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function AtomicFactsDrawer({ symbol, open, onClose }: AtomicFactsDrawerProps) {
  const drawerRef = useRef<HTMLElement>(null)
  const closeBtnRef = useRef<HTMLButtonElement>(null)
  const previouslyFocused = useRef<HTMLElement | null>(null)

  // 打开时：记录当前焦点、聚焦关闭按钮、锁定 body 滚动
  useEffect(() => {
    if (!open) return
    previouslyFocused.current = document.activeElement as HTMLElement | null
    // 聚焦关闭按钮（下一帧确保 DOM 已挂载）
    const t = window.requestAnimationFrame(() => {
      closeBtnRef.current?.focus()
    })
    // 锁定 body 滚动
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.cancelAnimationFrame(t)
      document.body.style.overflow = prevOverflow
      // 关闭后恢复打开前焦点
      previouslyFocused.current?.focus?.()
    }
  }, [open])

  // Escape 关闭 + 焦点 trap（Tab/Shift+Tab 限制在抽屉内）
  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!open) return
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== 'Tab') return
      const drawer = drawerRef.current
      if (!drawer) return
      const focusables = Array.from(drawer.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (focusables.length === 0) {
        e.preventDefault()
        closeBtnRef.current?.focus()
        return
      }
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey) {
        if (active === first || !drawer.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else {
        // 正向 Tab：焦点在最后一个可聚焦元素或已离开 drawer 时，回到第一个
        if (active === last || !drawer.contains(active)) {
          e.preventDefault()
          first.focus()
        }
      }
    },
    [open, onClose],
  )

  useEffect(() => {
    if (!open) return
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onKeyDown])

  if (!open || !symbol) return null

  return (
    <div className={styles.drawerOverlay} onClick={onClose} role="presentation">
      <aside
        ref={drawerRef}
        className={styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-label="个股状态观察"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          ref={closeBtnRef}
          type="button"
          className={styles.drawerClose}
          onClick={onClose}
          aria-label="关闭状态观察"
        >
          ×
        </button>
        <div className={styles.drawerBody}>
          <AtomicFactsPanel symbol={symbol} variant="expanded" />
        </div>
      </aside>
    </div>
  )
}

export default AtomicFactsDrawer
