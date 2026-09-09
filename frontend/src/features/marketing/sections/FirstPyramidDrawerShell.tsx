// 第一金字塔 Drawer（Full Alignment V1）：
// UI shell 做到完整：搜索框 + 8 个维度分组 tab。
// 具体 99 字段由 M3.5 共享 presentation registry 接入，本轮明确标注 DATA PENDING，不伪造。
import { useEffect } from 'react'
import { FIRST_PYRAMID } from '../data/copy'
import styles from '../marketing.module.scss'

type Props = {
  open: boolean
  onClose: () => void
}

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
          {/* 搜索 shell（V1 仅外壳，字段内容 M3.5 接入） */}
          <div className={styles.fieldSearch}>
            <input
              type="search"
              className={styles.fieldSearchInput}
              placeholder={drawer.searchPlaceholder}
              aria-label={drawer.searchPlaceholder}
            />
          </div>

          {/* 8 个维度分组 tab */}
          <ul className={styles.fieldGroups}>
            {drawer.groups.map((g, i) => (
              <li
                key={g.key}
                className={i === 0 ? styles.fieldGroupActive : styles.fieldGroup}
              >
                {g.label}
              </li>
            ))}
          </ul>

          {/* DATA PENDING：不伪造 99 项 */}
          <div className={styles.fieldPending} role="note">
            <span className={styles.fieldPendingTag}>DATA PENDING</span>
            <p className={styles.fieldNote}>{drawer.pendingNote}</p>
          </div>
        </div>
      </aside>
    </div>
  )
}
