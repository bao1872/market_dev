import BrandLogo from '@/components/BrandLogo'
import { FOOTER } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingFooter() {
  return (
    <footer className={styles.footer} data-testid="marketing-footer">
      <div className={styles.container}>
        <div className={styles.footerInner}>
          <div>
            <BrandLogo variant="footer" />
            <p className={styles.footerDesc}>{FOOTER.brand.description}</p>
          </div>
          {FOOTER.columns.map((col) => (
            <div key={col.title}>
              <h3 className={styles.footerTitle}>{col.title}</h3>
              <ul className={styles.footerList}>
                {col.links.map((link) => (
                  <li key={`${link.href}-${link.label}`}>
                    <a className={styles.footerLink} href={link.href}>
                      {link.label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          <div>
            <h3 className={styles.inviteTitle}>{FOOTER.invitation.title}</h3>
            <p className={styles.footerDesc}>{FOOTER.invitation.desc}</p>
          </div>
        </div>
        <div className={styles.footerBottom}>{FOOTER.copyright}</div>
      </div>
    </footer>
  )
}
