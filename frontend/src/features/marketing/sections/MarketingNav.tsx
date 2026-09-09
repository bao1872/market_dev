// 顶部导航（参考图 #1 对齐）：
// - 左：品牌 + "从零了解盘迹" 副标
// - 中：5 项菜单（窄屏折叠到汉堡按钮）
// - 右：绿色 "立即使用" CTA
// - 数据全部来自 data/copy.ts NAV；样式在 marketing.module.scss。
import { useState } from 'react'
import clsx from 'clsx'
import BrandLogo from '@/components/BrandLogo'
import { NAV } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingNav() {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <header className={styles.nav} data-testid="marketing-nav">
      <div className={styles.container}>
        <div className={styles.navInner}>
          <a className={styles.navBrand} href="/" aria-label="盘迹首页">
            <BrandLogo variant="landing" />
            <span className={styles.navTagline}>{NAV.tagline}</span>
          </a>

          <nav
            className={styles.navLinks}
            aria-label="主导航"
            data-mobile-open={mobileOpen ? 'true' : 'false'}
          >
            {NAV.items.map((item) => (
              <a key={item.href} className={styles.navLink} href={item.href}>
                {item.label}
              </a>
            ))}
          </nav>

          <div className={styles.navRight}>
            <a
              className={clsx(styles.btn, styles.btnPrimary, styles.navCta)}
              href={NAV.ctaHref}
              data-testid="marketing-nav-cta"
            >
              {NAV.ctaLabel}
            </a>
            <button
              className={styles.navBurger}
              type="button"
              aria-label="打开菜单"
              aria-expanded={mobileOpen}
              onClick={() => setMobileOpen((v) => !v)}
              data-testid="marketing-nav-burger"
            >
              <span aria-hidden="true">{mobileOpen ? '✕' : '☰'}</span>
            </button>
          </div>
        </div>
      </div>
    </header>
  )
}