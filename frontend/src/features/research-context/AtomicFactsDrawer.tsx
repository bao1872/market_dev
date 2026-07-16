// [AtomicFactsDrawer] - 描述: /stock/:symbol 状态观察右侧 overlay 抽屉
// 点击「显示状态观察」后打开；宽度 min(1080px, calc(100vw - 48px))，固定 overlay 不压缩主 K 线。
// 宽屏 4 列 / 普通桌面 2 列 / 小屏 1 列（由内部 grid 响应式）。
// 下方全宽近期变化 + 「更多观察」默认收起渲染 8 项 Aux（T3/T6/V1 永不出现）。
// 关闭方式：Escape / 点击遮罩 / 关闭按钮；role=dialog aria-modal 完整。
import { useEffect } from 'react'
import { AtomicFactsPanel } from './AtomicFactsPanel'
import styles from './AtomicFactsPanel.module.scss'

interface AtomicFactsDrawerProps {
  symbol: string | undefined
  open: boolean
  onClose: () => void
}

export function AtomicFactsDrawer({ symbol, open, onClose }: AtomicFactsDrawerProps) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open || !symbol) return null

  return (
    <div className={styles.drawerOverlay} onClick={onClose} role="presentation">
      <aside
        className={styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-label="个股状态观察"
        onClick={(e) => e.stopPropagation()}
      >
        <button
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
