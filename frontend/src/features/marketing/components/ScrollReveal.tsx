import { useEffect, useRef, useState, type ReactNode } from 'react'
import clsx from 'clsx'
import styles from '../marketing.module.scss'

interface ScrollRevealProps {
  children: ReactNode
  className?: string
  /** 错峰延迟（毫秒），用于卡片 stagger */
  delay?: number
}

// 进场动画：IntersectionObserver 触发一次后断开观察。
// prefers-reduced-motion 时直接渲染终态，不做位移与淡入。
export default function ScrollReveal({ children, className, delay = 0 }: ScrollRevealProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node) return

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setVisible(true)
      return
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setVisible(true)
            observer.disconnect()
          }
        }
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return (
    <div
      ref={ref}
      data-reveal={visible ? 'in' : 'out'}
      className={clsx(styles.reveal, visible && styles.revealIn, className)}
      style={delay > 0 ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </div>
  )
}
