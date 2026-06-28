// [门户] - 描述: 页脚组件（IP/二维码/更新记录/协议链接/版权）
// 协议链接点击触发 onLegalClick 回调，由父组件打开 LegalModal
// TODO: 运营主体、联系方式、第三方服务清单、退款规则待配置，不虚构主体信息
import { updateRecords, type LegalType } from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

export interface LandingFooterProps {
  onLegalClick: (type: LegalType) => void
}

export default function LandingFooter({ onLegalClick }: LandingFooterProps) {
  return (
    <footer className={styles.footer} id="about">
      <div className={`${styles.container} ${styles.footerGrid}`}>
        {/* IP 形象区 */}
        <div className={styles.ipRow}>
          <div className={styles.avatar}></div>
          <div>
            <h4>久而韭之</h4>
            <p>专注量化与产业研究的内容IP</p>
          </div>
        </div>

        {/* 二维码区 */}
        <div className={styles.qrWrap}>
          <div className={styles.qr}>
            <div className={styles.qrBox}></div>
            <span>公众号</span>
          </div>
          <div className={styles.qr}>
            <div className={styles.qrBox}></div>
            <span>视频号</span>
          </div>
        </div>

        {/* 更新记录 + 协议链接 */}
        <div>
          <h4>更新记录</h4>
          <div className={styles.updates}>
            {updateRecords.map((r, i) => (
              <div key={i} className={styles.updateRow}>
                <b>{r.version}</b>
                <span>{r.note}</span>
                <time>{r.date}</time>
              </div>
            ))}
          </div>
          <div className={styles.legalLinks}>
            <a href="#" onClick={(e) => { e.preventDefault(); onLegalClick('terms') }}>服务协议</a>
            <a href="#" onClick={(e) => { e.preventDefault(); onLegalClick('privacy') }}>隐私政策</a>
            <a href="#" onClick={(e) => { e.preventDefault(); onLegalClick('risk') }}>风险提示</a>
          </div>
        </div>

        {/* 版权信息 */}
        <div className={styles.copyright}>
          <div>© 2026 盘迹 PanJi</div>
          <div>All Rights Reserved.</div>
        </div>
      </div>
    </footer>
  )
}
