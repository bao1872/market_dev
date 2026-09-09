// Hero 真实产品大屏（V1.2/V1.4）：桌面产品截图放进 MacBook-style 外壳，
// 移动研究图放进 iPhone-style 外壳，两设备叠放成一台电脑 + 一台手机的整体产品展示。
// 不使用 Apple logo，只是通用 device shell。素材来自 MARKETING_MEDIA SSOT。
// mode=backdrop（V1.4）：作为绝对定位背景层的视觉容器，交换到 deviceStageBackdrop 布局。
import { MARKETING_MEDIA } from '../data/copy'
import styles from '../marketing.module.scss'

type Props = {
  mode?: 'standalone' | 'backdrop'
}

export default function ProductDeviceStage({ mode = 'standalone' }: Props) {
  return (
    <figure
      className={
        mode === 'backdrop' ? styles.deviceStageBackdrop : styles.deviceStage
      }
      aria-label="盘迹真实产品界面"
      data-testid="marketing-product-device-stage"
    >
      <div className={styles.macbookMock}>
        <div className={styles.macbookLid}>
          <img
            className={styles.macbookScreen}
            src={MARKETING_MEDIA.desktopProduct}
            alt="盘迹桌面研究终端真实界面"
            fetchPriority="high"
          />
        </div>
        <div className={styles.macbookBase} aria-hidden="true" />
      </div>

      <div className={styles.iphoneMock}>
        <div className={styles.iphoneIsland} aria-hidden="true" />
        <img
          className={styles.iphoneScreen}
          src={MARKETING_MEDIA.mobileResearch}
          alt="盘迹生成的移动研究图片真实界面"
        />
      </div>

      {mode !== 'backdrop' && (
        <figcaption className={styles.deviceCaption}>
          桌面研究终端 · 移动研究图片均来自真实盘迹产品
        </figcaption>
      )}
    </figure>
  )
}