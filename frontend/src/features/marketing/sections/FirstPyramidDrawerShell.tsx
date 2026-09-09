import { useEffect } from 'react'
import { FIRST_PYRAMID } from '../data/copy'
import styles from '../marketing.module.scss'

type Props = {
  open: boolean
  onClose: () => void
}

// 右侧 Drawer：默认不渲染，未打开时不占主体 layout height。
// M1.1 仅渲染外壳与维度分组；禁止在此复制 99 字段定义——M3.5 从产品侧共享 presentation registry 接入。
export default function FirstPyramidDrawerShell({ open, onClose }: Props) {
  useEffect(() => {
    if (!open) return

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  const { drawer } = FIRST_PYRAMID

  return (
    <div
      className={styles.drawerOverlay}
      data-testid="marketing-field-drawer-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose()
        }
      }}
    >
      <aside
        className={styles.fieldDrawer}
        role="dialog"
        aria-modal="true"
        aria-label={drawer.ariaLabel}
        data-testid="marketing-field-drawer"
      >
        <div className={styles.fieldDrawerHead}>
          <div>
            <h2 className={styles.fieldDrawerTitle}>{drawer.title}</h2>
            <p className={styles.fieldDrawerSubtitle}>{drawer.subtitle}</p>
          </div>
          <button
            type="button"
            className={styles.fieldDrawerClose}
            onClick={onClose}
            aria-label={drawer.closeLabel}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
        <div className={styles.fieldDrawerBody}>
          <ul className={styles.fieldGroups}>
            {drawer.groups.map((g) => (
              <li key={g.key} className={styles.fieldGroup}>
                {g.label}
              </li>
            ))}
          </ul>
          <p className={styles.fieldNote}>{drawer.placeholder}</p>
        </div>
      </aside>
    </div>
  )
}
