import clsx from 'clsx'
import BrandLogo from '@/components/BrandLogo'
import { NAV } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingNav() {
  return (
    <header className={styles.nav} data-testid="marketing-nav">
      <div className={styles.container}>
        <div className={styles.navInner}>
          <a className={styles.navBrand} href="/" aria-label="盘迹首页">
            <BrandLogo variant="landing" />
          </a>
          <nav className={styles.navLinks}>
            {NAV.items.map((item) => (
              <a key={item.href} className={styles.navLink} href={item.href}>
                {item.label}
              </a>
            ))}
          </nav>
          <a
            className={clsx(styles.btn, styles.btnPrimary, styles.navCta)}
            href={NAV.loginHref}
          >
            {NAV.loginLabel}
          </a>
        </div>
      </div>
    </header>
  )
}
