// 顶部导航（Full Alignment V1）：
// - 左：品牌 + "从零了解盘迹" 副标
// - 中：5 项真实菜单（窄屏折叠到汉堡按钮，打开为独立 mobile panel）
// - 右：绿色 "开始使用" CTA
// 移动端面板必须真正可用：fixed 展开、点击锚点收起、Escape 关闭。
import { useEffect, useState } from 'react'
import clsx from 'clsx'
import BrandLogo from '@/components/BrandLogo'
import { NAV } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingNav() {
  const [mobileOpen, setMobileOpen] = useState(false)

  // Escape 关闭移动端菜单
  useEffect(() => {
    if (!mobileOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMobileOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mobileOpen])

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
              aria-label={mobileOpen ? '关闭菜单' : '打开菜单'}
              aria-expanded={mobileOpen}
              onClick={() => setMobileOpen((v) => !v)}
              data-testid="marketing-nav-burger"
            >
              <span aria-hidden="true">{mobileOpen ? '✕' : '☰'}</span>
            </button>
          </div>
        </div>
      </div>

      {/* 移动端独立面板：fixed 展开于 sticky nav 之下；点击锚点 / Escape 关闭 */}
      {mobileOpen ? (
        <div
          className={styles.mobileNavPanel}
          role="dialog"
          aria-modal="true"
          aria-label="移动端主导航"
        >
          <nav className={styles.mobileNavLinks} aria-label="移动端主导航">
            {NAV.items.map((item) => (
              <a
                key={item.href}
                className={styles.mobileNavLink}
                href={item.href}
                onClick={() => setMobileOpen(false)}
              >
                {item.label}
              </a>
            ))}
          </nav>
          <a
            className={clsx(styles.btn, styles.btnPrimary, styles.mobileNavCta)}
            href={NAV.ctaHref}
            onClick={() => setMobileOpen(false)}
          >
            {NAV.ctaLabel}
          </a>
        </div>
      ) : null}
    </header>
  )
}
