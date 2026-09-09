// Smooth Market Replay（V1.3）：把「canonical frame」与「视觉播放」解耦。
// canonical frame 负责结构计算状态（离散，约 100 帧）；visual timeline 负责 K 线平滑推进。
// 总播放约 52s，tick 约 170ms（每秒 5~6 次画面更新），每次约 1~2 根 bar，
// 避免旧版「一秒突然跳 5 根日 K」的顿挫。
//
// 遵守 reduce-motion：用户偏好减弱动效时不自动播放。
import { useCallback, useEffect, useRef, useState } from 'react'

export const TOTAL_DURATION_MS = 52_000
export const TICK_MS = 170

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

/** 给定 endIndex 计算 elapsed 毫秒（无未来：只允许 [start, end] 范围）。 */
function elapsedMsFor(
  endIndex: number,
  startEndIndex: number,
  endEndIndex: number,
): number {
  const span = Math.max(1, endEndIndex - startEndIndex)
  const clamped = Math.max(startEndIndex, Math.min(endEndIndex, endIndex))
  return ((clamped - startEndIndex) / span) * TOTAL_DURATION_MS
}

export function useSmoothMarketReplay({
  startEndIndex,
  endEndIndex,
  enabled,
}: {
  startEndIndex: number
  endEndIndex: number
  enabled: boolean
}) {
  const [elapsedMs, setElapsedMs] = useState(0)
  const [playing, setPlaying] = useState(false)

  const baseElapsed = useRef(0)
  const startedAt = useRef<number | null>(null)
  const autoPlayed = useRef(false)

  const reducedMotion = usePrefersReducedMotion()

  const progress = Math.min(1, elapsedMs / TOTAL_DURATION_MS)

  const visibleEndIndex = Math.round(
    startEndIndex + progress * (endEndIndex - startEndIndex),
  )

  const play = useCallback(() => {
    if (baseElapsed.current >= TOTAL_DURATION_MS) {
      const resetTo = elapsedMsFor(startEndIndex, startEndIndex, endEndIndex)
      baseElapsed.current = resetTo
      setElapsedMs(resetTo)
    }
    startedAt.current = performance.now()
    setPlaying(true)
  }, [startEndIndex, endEndIndex])

  const pause = useCallback(() => {
    if (startedAt.current != null) {
      baseElapsed.current = Math.min(
        TOTAL_DURATION_MS,
        baseElapsed.current + performance.now() - startedAt.current,
      )
    }
    startedAt.current = null
    setElapsedMs(baseElapsed.current)
    setPlaying(false)
  }, [])

  const replay = useCallback(() => {
    baseElapsed.current = 0
    startedAt.current = null
    setElapsedMs(0)
    setPlaying(true)
  }, [])

  const seekToEndIndex = useCallback(
    (endIndex: number) => {
      const target = elapsedMsFor(endIndex, startEndIndex, endEndIndex)
      baseElapsed.current = target
      startedAt.current = null
      setElapsedMs(target)
      setPlaying(false)
    },
    [startEndIndex, endEndIndex],
  )

  // 进入视口且未 reduce-motion 时自动播放一次。
  useEffect(() => {
    if (!enabled) {
      setPlaying(false)
      return
    }
    if (!reducedMotion && !autoPlayed.current) {
      if (baseElapsed.current >= TOTAL_DURATION_MS) baseElapsed.current = 0
      autoPlayed.current = true
      setPlaying(true)
    }
  }, [enabled, reducedMotion])

  // 播放推进：基于真实经过的 wall-clock 累加，而非每帧固定步长。
  useEffect(() => {
    if (!enabled || !playing) return
    if (startedAt.current == null) {
      startedAt.current = performance.now()
    }
    const id = window.setInterval(() => {
      const elapsed = Math.min(
        TOTAL_DURATION_MS,
        baseElapsed.current + performance.now() - (startedAt.current ?? performance.now()),
      )
      setElapsedMs(elapsed)
      if (elapsed >= TOTAL_DURATION_MS) {
        baseElapsed.current = TOTAL_DURATION_MS
        startedAt.current = null
        setPlaying(false)
      }
    }, TICK_MS)
    return () => window.clearInterval(id)
  }, [enabled, playing])

  // 页面切到后台时暂停，切回后不自动恢复。
  useEffect(() => {
    if (typeof document === 'undefined') return
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') setPlaying(false)
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [])

  return {
    progress,
    visibleEndIndex,
    playing,
    play,
    pause,
    replay,
    seekToEndIndex,
  }
}