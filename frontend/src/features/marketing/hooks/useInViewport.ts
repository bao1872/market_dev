import { useEffect, useRef, useState } from 'react'

// 教学动画只在进入 viewport 后才允许自动播放：
// 避免页面一加载两个动画就在后台自己跑完。
export function useInViewport<T extends Element>(threshold = 0.35) {
  const ref = useRef<T | null>(null)
  const [inView, setInView] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node) return

    if (!('IntersectionObserver' in window)) {
      setInView(true)
      return
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        setInView(entry.isIntersecting)
      },
      {
        threshold,
        rootMargin: '0px 0px -8% 0px',
      },
    )

    observer.observe(node)

    return () => observer.disconnect()
  }, [threshold])

  return { ref, inView }
}
