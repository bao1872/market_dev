// [V1.6.1] Footer：彻底重排为 3 层（CTA / 主体 / bottom），删除所有旧 footer CSS。
import clsx from 'clsx'
import BrandLogo from '@/components/BrandLogo'
import { FOOTER } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingFooter() {
  const f = FOOTER
  return (
    <footer
      className={clsx(styles.footer)}
      id={f.community.id}
      data-testid="marketing-footer"
    >
      <div className={styles.container}>
        {/* 第 1 层：CTA */}
        <div className={styles.footerAction}>
          <div>
            <h2>{f.cta.title}</h2>
            <p>{f.cta.subtitle}</p>
          </div>
          <a
            className={clsx(styles.btn, styles.btnPrimary)}
            href={f.cta.button.href}
          >
            {f.cta.button.label}
          </a>
        </div>

        {/* 第 2 层：主体（brand + columns + QR） */}
        <div className={styles.footerBody}>
          <div className={styles.footerBrand}>
            <BrandLogo variant="footer" />
            <p className={styles.footerDesc}>
              先找变化，再缩范围。
              <br />
              值得继续看的，留下来跟。
            </p>
          </div>

          {f.columns.map((col) => (
            <nav key={col.title} className={styles.footerColumn}>
              <h3>{col.title}</h3>
              {col.links.map((link) =>
                link.href ? (
                  <a
                    key={link.label}
                    href={link.href}
                    {...(link.external
                      ? { target: '_blank', rel: 'noopener noreferrer' }
                      : {})}
                  >
                    {link.label}
                  </a>
                ) : (
                  <span key={link.label}>{link.label}</span>
                ),
              )}
            </nav>
          ))}

          <div className={styles.footerCommunity}>
            {f.community.cards.map((card) => {
              const inner = (
                <>
                  <div className={styles.footerQrImage}>
                    <img src={card.imageSrc} alt={card.imageAlt} />
                  </div>
                  <strong>{card.title}</strong>
                  <span>{card.subtitle}</span>
                </>
              )
              return card.href ? (
                <a
                  key={card.id}
                  className={styles.footerQrCompact}
                  href={card.href}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {inner}
                </a>
              ) : (
                <div key={card.id} className={styles.footerQrCompact}>
                  {inner}
                </div>
              )
            })}
          </div>
        </div>

        {/* 第 3 层：bottom */}
        <div className={styles.footerBottom}>{f.copyright}</div>
      </div>
    </footer>
  )
}
