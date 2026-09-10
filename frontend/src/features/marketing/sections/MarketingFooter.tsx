// MarketingFooter（V1.5.2 统一 Footer / UNIFIED FOOTER）。
// [V1.5] 社区出口：id="community"，底部 QQ 群 + 雪球主页双二维码。card.href 存在时可点击打开。
// [V1.5.1] 删除重复雪球说明（FOOTER.invitation）；二维码 object-fit: contain，不 overlay/mask/filter/圆角。
// [V1.5.2] 去掉独立 boxed footerCommunity 大卡片，并入一整块 footerUnified：
//   桌面：所有文字集中左侧/左下，两个小二维码位于右下；
//   430：文字在上、两个约 104px 二维码并排在最下。
//   二维码下仅保留卡片的 title + subtitle，不再在码下附说明（说明已在左侧 community copy）。
//   禁止重新裁 canonical QR asset（QQ 保持 922×922），这里只控制页面显示尺寸。
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
        <div className={styles.footerUnified}>
          <div className={styles.footerLeft}>
            <div className={styles.footerMain}>
              <div className={styles.footerBrand}>
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
            </div>

            <div className={styles.footerCommunityCopy}>
              <h3 className={styles.footerCommunityTitle}>
                {FOOTER.community.title}
              </h3>
              <p className={styles.footerCommunityDesc}>
                {FOOTER.community.desc}
              </p>
            </div>
          </div>

          <div className={styles.footerQrGrid}>
            {FOOTER.community.cards.map((card) => {
              const body = (
                <>
                  <div className={styles.footerQrImage}>
                    <img
                      src={card.imageSrc}
                      alt={card.imageAlt}
                      loading="lazy"
                    />
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
                  {body}
                </a>
              ) : (
                <div key={card.id} className={styles.footerQrCompact}>
                  {body}
                </div>
              )
            })}
          </div>
        </div>

        <div className={styles.footerBottom}>{FOOTER.copyright}</div>
      </div>
    </footer>
  )
}