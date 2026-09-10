// MarketingFooter（Full Alignment V1 + V1.5 社区出口）。
// [V1.5] Footer 升级为社区出口：id="community"，底部新增「一起交流」双二维码区
// （QQ 群 + 雪球主页）。card.href 存在时整图可点击（PC 用户不扫码也能打开）。
// 保留原有 Brand + footer columns + 底行 copyright。不 overlay 文字/mask/filter 于二维码。
import BrandLogo from '@/components/BrandLogo'
import { FOOTER } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingFooter() {
  return (
    <footer
      className={styles.footer}
      id={FOOTER.community.id}
      data-testid="marketing-footer"
    >
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

        {/* [V1.5] 社区出口：双二维码 */}
        <div className={styles.footerCommunity}>
          <div className={styles.footerCommunityCopy}>
            <h3 className={styles.footerCommunityTitle}>{FOOTER.community.title}</h3>
            <p className={styles.footerCommunityDesc}>{FOOTER.community.desc}</p>
          </div>
          <div className={styles.footerQrGrid}>
            {FOOTER.community.cards.map((card) => (
              <article key={card.id} className={styles.footerQrCard}>
                {card.href ? (
                  <a
                    className={styles.footerQrImage}
                    href={card.href}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <img src={card.imageSrc} alt={card.imageAlt} loading="lazy" />
                  </a>
                ) : (
                  <div className={styles.footerQrImage}>
                    <img src={card.imageSrc} alt={card.imageAlt} loading="lazy" />
                  </div>
                )}
                <strong className={styles.footerQrCardTitle}>{card.title}</strong>
                <span className={styles.footerQrCardSubtitle}>{card.subtitle}</span>
                <small className={styles.footerQrCardNote}>{card.note}</small>
              </article>
            ))}
          </div>
        </div>

        <div className={styles.footerBottom}>{FOOTER.copyright}</div>
      </div>
    </footer>
  )
}