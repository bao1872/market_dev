// [BrandLogo] - 描述: 唯一品牌标识组件（SVG），页眉/页尾/业务侧栏全部复用
// 视觉真源：ref/盘迹品牌视觉资产包_v1.0/01_标志系统 + README.md
// 标志规范：四节点折线路径 + 末端高亮共识节点（莹感绿体系）
// 不变形、不旋转、不增减节点、不替换颜色
import clsx from 'clsx'
import styles from './BrandLogo.module.scss'

export type BrandLogoVariant = 'sidebar' | 'landing' | 'footer'

export interface BrandLogoProps {
  variant: BrandLogoVariant
  className?: string
}

// 核心图形：四节点折线（呼应 Node Cluster）+ 末端高亮共识节点（莹感绿圆环）
// 三个非末端节点为低饱和雾白，末端节点为品牌主色高亮圆环
// sidebar variant 仅渲染图形；landing/footer variant 渲染图形 + "盘迹" 文字
export default function BrandLogo({ variant, className }: BrandLogoProps) {
  const showText = variant !== 'sidebar'
  return (
    <span className={clsx(styles.root, styles[variant], className)}>
      <svg
        className={styles.mark}
        viewBox="0 0 32 32"
        role="img"
        aria-label="盘迹"
        focusable="false"
      >
        {/* 四节点折线：起点→第二点→第三点→末端（上升趋势） */}
        <polyline
          points="5,22 12,17 18,14 27,8"
          fill="none"
          stroke="#F2F6F8"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity="0.92"
        />
        {/* 节点 1：起点（小） */}
        <circle cx="5" cy="22" r="1.6" fill="#F2F6F8" opacity="0.7" />
        {/* 节点 2：中段（小） */}
        <circle cx="12" cy="17" r="1.6" fill="#F2F6F8" opacity="0.7" />
        {/* 节点 3：中段（小） */}
        <circle cx="18" cy="14" r="1.6" fill="#F2F6F8" opacity="0.85" />
        {/* 节点 4：末端共识节点（品牌高亮圆环 + 实心点） */}
        <circle cx="27" cy="8" r="3.2" fill="none" stroke="#00F6C2" strokeWidth="1.6" />
        <circle cx="27" cy="8" r="1.8" fill="#00F6C2" />
      </svg>
      {showText && <span className={styles.text}>盘迹</span>}
    </span>
  )
}
