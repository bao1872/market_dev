// 第一金字塔 Drawer（Marketing V1.5.2 / M3.5 接线完成）：
// 直接消费产品 SSOT 派生的 MARKETING_FIRST_PYRAMID_FIELDS（99 字段）。
// 布局：左 8 个分组按钮（含字段数）→ 右侧当前分组字段 title + description。
// 搜索跨 99 字段匹配（title + description），搜索时显示每条所属分组 label。
// 默认打开「趋势」而不是「快照」，避免一上来就看到交易日/来源等元数据。
// 完全静态：整个字典内容均来自 public adapter，禁止远端动态取数与外部字段规格 hook。
import { useEffect, useMemo, useState } from 'react'
import { FIRST_PYRAMID } from '../data/copy'
import { MARKETING_FIRST_PYRAMID_FIELDS } from '../data/firstPyramidPublicDictionary'
import styles from '../marketing.module.scss'

type Props = {
  open: boolean
  onClose: () => void
}

export default function FirstPyramidDrawerShell({ open, onClose }: Props) {
  const [query, setQuery] = useState('')
  const [activeGroup, setActiveGroup] = useState('trend')

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

  // 所有 hooks 必须在条件 return 之前调用，否则 open 切换会改变 hook 数量
  // 触发 React error #310（Rendered more hooks than during the previous render）。
  const { drawer } = FIRST_PYRAMID
  const keyword = query.trim().toLowerCase()

  const matchedFields = useMemo(() => {
    if (!keyword) {
      const activeLabel =
        drawer.groups.find((group) => group.key === activeGroup)?.label ?? ''
      return MARKETING_FIRST_PYRAMID_FIELDS.filter(
        (field) => field.group === activeLabel,
      )
    }

    return MARKETING_FIRST_PYRAMID_FIELDS.filter((field) =>
      `${field.title} ${field.description}`.toLowerCase().includes(keyword),
    )
  }, [keyword, activeGroup, drawer.groups])

  if (!open) return null

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
          <div className={styles.fieldSearch}>
            <input
              type="search"
              className={styles.fieldSearchInput}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={drawer.searchPlaceholder}
              aria-label={drawer.searchPlaceholder}
            />
          </div>

          <div className={styles.fieldDictionary}>
            <nav className={styles.fieldGroups} aria-label="第一金字塔字段分组">
              {drawer.groups.map((group) => {
                const count = MARKETING_FIRST_PYRAMID_FIELDS.filter(
                  (field) => field.group === group.label,
                ).length

                return (
                  <button
                    key={group.key}
                    type="button"
                    className={
                      activeGroup === group.key
                        ? styles.fieldGroupActive
                        : styles.fieldGroup
                    }
                    onClick={() => setActiveGroup(group.key)}
                  >
                    <span>{group.label}</span>
                    <small>{count}</small>
                  </button>
                )
              })}
            </nav>

            <div className={styles.fieldList}>
              {matchedFields.map((field) => (
                <article key={field.key} className={styles.fieldRow}>
                  {keyword && (
                    <span className={styles.fieldRowGroup}>{field.group}</span>
                  )}
                  <strong>{field.title}</strong>
                  <p>{field.description}</p>
                </article>
              ))}

              {matchedFields.length === 0 && (
                <p className={styles.fieldEmpty}>没找到相关字段。</p>
              )}
            </div>
          </div>
        </div>
      </aside>
    </div>
  )
}