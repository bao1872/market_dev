import { useEffect, useMemo, useRef, useState } from 'react'

// reduced-motion 只禁止"自动播放"；用户主动点 Play / Replay 仍允许播放。
export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false)

  useEffect(() => {
    if (!window.matchMedia) return

    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const sync = () => setReduced(media.matches)

    sync()
    media.addEventListener('change', sync)

    return () => media.removeEventListener('change', sync)
  }, [])

  return reduced
}

type Options = {
  frameCount: number
  /** 每个阶段结束时的 frame（1-based，等于已展示 K 线根数） */
  stageEnds: number[]
  /** 是否允许播放（由 section 是否进入 viewport 决定） */
  enabled: boolean

  intervalMs?: number
  initialFrame?: number
}

export function useStoryPlayer({
  frameCount,
  stageEnds,
  enabled,
  intervalMs = 420,
  initialFrame = 1,
}: Options) {
  const reducedMotion = usePrefersReducedMotion()

  const [frame, setFrame] = useState(initialFrame)
  const [playing, setPlaying] = useState(false)

  const autoStartedRef = useRef(false)

  const stageIndex = useMemo(() => {
    const found = stageEnds.findIndex((end) => frame <= end)
    return found === -1 ? stageEnds.length - 1 : found
  }, [frame, stageEnds])

  // 第一次进入 viewport 才自动播放；离开 viewport 暂停
  useEffect(() => {
    if (!enabled) {
      setPlaying(false)
      return
    }

    if (!reducedMotion && !autoStartedRef.current && frame < frameCount) {
      autoStartedRef.current = true
      setPlaying(true)
    }
  }, [enabled, reducedMotion, frame, frameCount])

  useEffect(() => {
    if (!playing || !enabled) return

    const timer = window.setInterval(() => {
      setFrame((current) => Math.min(current + 1, frameCount))
    }, intervalMs)

    return () => window.clearInterval(timer)
  }, [playing, enabled, intervalMs, frameCount])

  useEffect(() => {
    if (frame >= frameCount) {
      setPlaying(false)
    }
  }, [frame, frameCount])

  const previousStage = () => {
    setPlaying(false)

    if (stageIndex <= 0) {
      setFrame(initialFrame)
      return
    }

    setFrame(stageEnds[stageIndex - 1])
  }

  const nextStage = () => {
    setPlaying(false)

    const nextIndex = Math.min(stageIndex + 1, stageEnds.length - 1)
    setFrame(stageEnds[nextIndex])
  }

  const replay = () => {
    setFrame(initialFrame)
    setPlaying(enabled)
  }

  return {
    frame,
    playing,
    stageIndex,
    reducedMotion,

    play: () => setPlaying(true),
    pause: () => setPlaying(false),

    previousStage,
    nextStage,
    replay,
  }
}
