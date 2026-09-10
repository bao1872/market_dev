// MarketingFooter（V1.6 · FinalCTA 并入）：
// 顶部：一行 CTA（标题 + 开始使用按钮），彻底删除独立 FinalCTA section。
// 下方：原 unified footer（brand + columns + 二维码）；删除 footerCommunityCopy 重复说明。
import clsx from 'clsx'
import BrandLogo from '@/components/BrandLogo'
import { FOOTER } from '../data/copy'
import styles from '../marketing.module.scss'

export default function MarketingFooter() {
  return (
    <footer
      className={clsx(styles.footer, styles.footerUnified)}
      id={FOOTER.community.id}
      data-testid="marketing-footer"
    >
      <div className={styles.container}>
        {/* [V1.6] 顶部 CTA 行 — 替代已删除的独立 FinalCTA section */}
        <div className={styles.footerCtaRow}>
          <div>
            <h2>{FOOTER.cta.title}</h2>
            <p>{FOOTER.cta.subtitle}</p>
          </div>
          <a
            className={clsx(styles.btn, styles.btnPrimary, styles.footerCtaBtn)}
            href={FOOTER.cta.button.href}
          >
            {FOOTER.cta.button.label}
          </a>
        </div>

        {/* 分隔线 */}
        <div className={styles.footerDivider} />

        {/* 下方 unified footer（无独立 community 说明卡片） */}
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
