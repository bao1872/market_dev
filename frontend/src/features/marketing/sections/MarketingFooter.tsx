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
                  <li key={`${link.href ?? 'text'}-${link.label}`}>
                    {link.href ? (
                      <a
                        className={styles.footerLink}
                        href={link.href}
                        {...(link.external
                          ? { target: '_blank', rel: 'noopener noreferrer' }
                          : {})}
                      >
                        {link.label}
                      </a>
                    ) : (
                      <span className={styles.footerText}>
                        {link.label}
                        {link.note ? (
                          <em className={styles.footerNote}>{link.note}</em>
                        ) : null}
                      </span>
                    )}
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
