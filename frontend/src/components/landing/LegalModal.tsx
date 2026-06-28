// [门户] - 描述: 法律条款模态框（服务协议/隐私政策/风险提示）
// 受控组件，内容由 landingData.legal 提供，用 dangerouslySetInnerHTML 渲染 legal HTML
import { legal, type LegalType } from '@/pages/LandingPage/landingData'
import styles from '@/pages/LandingPage/LandingPage.module.scss'

// 法律条款模态：open 控制显示，type 决定内容，onClose 关闭
export interface LegalModalProps {
  open: boolean
  type: LegalType | null
  onClose: () => void
}

export default function LegalModal({ open, type, onClose }: LegalModalProps) {
  if (!open || !type) return null
  const doc = legal[type]

  return (
    <div className={`${styles.modal} ${styles.open}`} onClick={onClose}>
      <div className={styles.modalCard} onClick={(e) => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <h3>{doc.title}</h3>
          <button className={styles.close} onClick={onClose} aria-label="关闭">×</button>
        </div>
        {/* legal 内容为静态 HTML 文本（非用户输入），用 dangerouslySetInnerHTML 渲染 */}
        <div
          className={styles.legalBody}
          dangerouslySetInnerHTML={{ __html: doc.html }}
        />
      </div>
    </div>
  )
}
