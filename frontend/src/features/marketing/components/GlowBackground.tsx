import clsx from 'clsx'
import styles from '../marketing.module.scss'

// 装饰性角落光斑，不响应指针事件，对屏幕阅读器隐藏
export default function GlowBackground() {
  return (
    <div aria-hidden="true">
      <div className={clsx(styles.glow, styles.glowA)} />
      <div className={clsx(styles.glow, styles.glowB)} />
    </div>
  )
}
