// [BrandLogo] - 描述: 唯一品牌标识组件（使用批准 PNG 资产），页眉/页尾/业务侧栏全部复用
// 视觉真源：ref/盘迹品牌视觉资产包_v1.0/01_标志系统（CHANGE-20260713-007）
// 运行资产位于 frontend/src/assets/brand/，ref 不作为运行时依赖。
// - sidebar variant：使用 logo_symbol_128.png（批准 symbol 资产，正方形）
// - landing/footer variant：使用 logo_horizontal_dark.png（批准 horizontal 资产，含"盘迹"文字）
// 不变形、不旋转、不增减节点、不替换颜色、不共享字体文件
// 禁止恢复手绘 SVG 或在组件中重新构造标志几何
import clsx from 'clsx'
import logoSymbol128 from '@/assets/brand/logo_symbol_128.png'
import logoHorizontalDark from '@/assets/brand/logo_horizontal_dark.png'
import styles from './BrandLogo.module.scss'

export type BrandLogoVariant = 'sidebar' | 'landing' | 'footer'

export interface BrandLogoProps {
  variant: BrandLogoVariant
  className?: string
}

// sidebar variant 仅渲染 symbol 资产；landing/footer variant 渲染 horizontal 资产（含文字）
// horizontal 资产本身已含"盘迹"文字 + 标语，无需额外渲染文字 span
export default function BrandLogo({ variant, className }: BrandLogoProps) {
  const isSidebar = variant === 'sidebar'
  const src = isSidebar ? logoSymbol128 : logoHorizontalDark
  const alt = '盘迹'
  return (
    <span className={clsx(styles.root, styles[variant], className)}>
      <img
        className={styles.mark}
        src={src}
        alt={alt}
        role="img"
        // 装饰性标志在 sidebar 场景由相邻 .brand-title 提供"盘迹"文字；
        // landing/footer 场景 horizontal 资产本身含文字，img alt 兜底
        aria-label={alt}
        draggable={false}
      />
    </span>
  )
}
