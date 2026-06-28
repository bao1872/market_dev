// [BrandLogo] - 描述: 唯一品牌标识组件（SVG），页眉/页尾/业务侧栏全部复用
// variant 控制尺寸与是否显示文字；favicon.svg 与本组件内嵌 SVG 同源
import clsx from 'clsx'
import styles from './BrandLogo.module.scss'

export type BrandLogoVariant = 'sidebar' | 'landing' | 'footer'

export interface BrandLogoProps {
  variant: BrandLogoVariant
  className?: string
}

// 核心图形：圆形蓝紫渐变背景 + 白色上升趋势折线 + 末端节点（呼应 Node Cluster）
// sidebar variant 仅渲染图形；landing/footer variant 渲染图形 + "策略主页" 文字
export default function BrandLogo({ variant, className }: BrandLogoProps) {
  const showText = variant !== 'sidebar'
  return (
    <span className={clsx(styles.root, styles[variant], className)}>
      <svg
        className={styles.mark}
        viewBox="0 0 32 32"
        role="img"
        aria-label="策略主页 Logo"
        focusable="false"
      >
        <defs>
          <linearGradient id="brandLogoGradient" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#2962ff" />
            <stop offset="100%" stopColor="#8b5cf6" />
          </linearGradient>
        </defs>
        <circle cx="16" cy="16" r="15" fill="url(#brandLogoGradient)" />
        <polyline
          points="6,21 12,15 17,18 26,9"
          fill="none"
          stroke="#ffffff"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="26" cy="9" r="2.4" fill="#ffffff" />
        <circle cx="6" cy="21" r="1.6" fill="#ffffff" opacity="0.85" />
      </svg>
      {showText && <span className={styles.text}>策略主页</span>}
    </span>
  )
}
