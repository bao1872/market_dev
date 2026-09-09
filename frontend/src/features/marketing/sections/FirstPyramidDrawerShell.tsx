import { useState } from 'react'
import clsx from 'clsx'
import SectionHeading from '../components/SectionHeading'
import { FIRST_PYRAMID } from '../data/copy'
import styles from '../marketing.module.scss'

// M1 仅渲染 Drawer 外壳与维度分组。
// 禁止在此复制 99 字段定义——M3.5 从产品侧共享 presentation registry 接入。
export default function FirstPyramidDrawerShell() {
  const [open, setOpen] = useState(false)

  return (
    <section
      className={clsx(styles.section, styles.sectionAlt)}
      id="fields"
      data-testid="marketing-fields"
    >
      <div className={styles.container}>
        <SectionHeading
          index={FIRST_PYRAMID.index}
          eyebrow={FIRST_PYRAMID.eyebrow}
          title={FIRST_PYRAMID.title}
          subtitle={FIRST_PYRAMID.subtitle}
        />
        <div className={styles.drawer}>
          <button
            type="button"
            className={styles.drawerTrigger}
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            <span>{FIRST_PYRAMID.drawer.triggerLabel}</span>
            <span className={styles.drawerCaret}>{open ? '收起' : '展开'}</span>
          </button>
          {open && (
            <div className={styles.drawerBody}>
              <ul className={styles.drawerGroups}>
                {FIRST_PYRAMID.drawer.groups.map((g) => (
                  <li key={g.key} className={styles.drawerGroup}>
                    {g.label}
                  </li>
                ))}
              </ul>
              <p className={styles.drawerNote}>{FIRST_PYRAMID.drawer.placeholder}</p>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
