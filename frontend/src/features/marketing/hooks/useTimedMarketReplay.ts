// 有节奏的真实结构回放（V1.2）：按总时长把 frames 均分推进。
// 与教学模型无关，只把 canonical SMC 的 48 帧按「约50秒/硬上限55秒」播放完。
// 遵守 reduce-motion：用户偏好减弱动效时不自动播放。
import { useEffect, useRef, useState } from 'react'

const TOTAL_DURATION_MS = 50_000

// 用一个稳定值取播放区间，避免每次渲染推导出不同的闭包。
const intervalMsFor = (frameCount: number) =>
  Math.floor(TOTAL_DURATION_MS / Math.max(frameCount - 1, 1))

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(mq.matches)
    update()
    mq.addEventListener('change', update)
    return () => mq.removeEventListener('change', update)
  }, [])

  return reduced
}

export function useTimedMarketReplay({
  frameCount,
  enabled,
}: {
  frameCount: number
  enabled: boolean
}) {
  const [frameIndex, setFrameIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const autoPlayedRef = useRef(false)
  const reducedMotion = usePrefersReducedMotion()

  const intervalMs = intervalMsFor(frameCount)

  // 进入视口时自动播放一次；reduced-motion 时仅停留在第一帧。
  useEffect(() => {
    if (!enabled) {
      setPlaying(false)
      return
    }
    if (!reducedMotion && !autoPlayedRef.current) {
      autoPlayedRef.current = true
      setPlaying(true)
    }
  }, [enabled, reducedMotion])

  // 播放推进：到末帧停。离开 section（enabled=false）时通过上面 effect 停。
  useEffect(() => {
    if (!enabled || !playing) return
    const id = window.setInterval(() => {
      setFrameIndex((current) => {
        if (current >= frameCount - 1) {
          setPlaying(false)
          return current
        }
        return current + 1
      })
    }, intervalMs)
    return () => window.clearInterval(id)
  }, [enabled, playing, intervalMs, frameCount])

  // 页面切到后台时暂停，切回后不自动恢复（用户在表格内手动继续）。
  useEffect(() => {
    if (typeof document === 'undefined') return
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') setPlaying(false)
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [])

  return {
    frameIndex,
    playing,
    play: () => setPlaying(true),
    pause: () => setPlaying(false),
    replay: () => {
      setFrameIndex(0)
      setPlaying(true)
    },
    previous: () => {
      setPlaying(false)
      setFrameIndex((i) => Math.max(0, i - 1))
    },
    next: () => {
      setPlaying(false)
      setFrameIndex((i) => Math.min(frameCount - 1, i + 1))
    },
  }
}