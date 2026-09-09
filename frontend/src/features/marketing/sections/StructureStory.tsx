// StructureStory（V1.2）— 真实结构回放。
// 不再使用 synthetic STRUCTURE_CANDLES/EVENTS/STAGES，
// 而是播放中际旭创 300308 近两年真实日线 + canonical SMC（一次性 frozen JSON）。
// - 数据从 MARKETING_MEDIA.structureReplay（静态 JSON）same-origin fetch，绝不调用公开行情接口
// - 每帧构建 IndicatorResponse，复用生产 StrategyChart + smcLabels 画结构标签
// - 滚动窗口用生产 createDefaultViewport(currentBars.length, 120)
import { useEffect, useMemo, useState } from 'react'
import { createDefaultViewport } from '@/components/chartViewport'
import type { IndicatorResponse } from '@/api/endpoints'
import SectionHeading from '../components/SectionHeading'
import ScrollReveal from '../components/ScrollReveal'
import RealStructureReplay from '../components/RealStructureReplay'
import { useInViewport } from '../hooks/useInViewport'
import { useTimedMarketReplay } from '../hooks/useTimedMarketReplay'
import { MARKETING_MEDIA, STRUCTURE_STORY } from '../data/copy'
import type { MarketingStructureReplay } from '../data/structureReplayTypes'
import styles from '../marketing.module.scss'

// 显示 preset 需要的指标视图。SMC DTO 由后端 canonical 直接产出，
// 与前端 IndicatorResponse['data'] 的泛型签名不同构，因此在数据边界做一次窄化 cast，
// 不再把整份 snapshot 塞成 as unknown as（详见 spec §Y adapter helper）。
function buildReplayIndicators(
  replay: MarketingStructureReplay,
  frame: MarketingStructureReplay['frames'][number],
): IndicatorResponse {
  return {
    layers: [replay.smcLayer],
    data: { smc: frame.smc } as IndicatorResponse['data'],
    errors: {},
    timeframe: replay.timeframe,
  }
}

export default function StructureStory() {
  // 接近视口才开始 fetch，避免 500-bar JSON 阻塞首屏。
  const { ref, inView } = useInViewport<HTMLDivElement>(0.05)
  const [replay, setReplay] = useState<MarketingStructureReplay | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!inView || replay || error) return
    let cancelled = false
    fetch(MARKETING_MEDIA.structureReplay)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json() as Promise<MarketingStructureReplay>
      })
      .then((data) => {
        if (!cancelled) setReplay(data)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '加载失败')
        }
      })
    return () => {
      cancelled = true
    }
  }, [inView, replay, error])

  const player = useTimedMarketReplay({
    frameCount: replay?.frames.length ?? 0,
    enabled: inView && !!replay,
  })

  const frame = replay?.frames[player.frameIndex]

  const currentBars = useMemo(
    () => (replay && frame ? replay.bars.slice(0, frame.endIndex) : []),
    [replay, frame],
  )

  const indicators = useMemo(
    () => (replay && frame ? buildReplayIndicators(replay, frame) : undefined),
    [replay, frame],
  )

  const viewport = useMemo(() => {
    if (!currentBars.length) return undefined
    return createDefaultViewport(currentBars.length, 120)
  }, [currentBars.length])

  return (
    <section
      className={styles.section}
      id="structure-story"
      data-testid="marketing-structure-story"
      ref={ref}
    >
      <div className={styles.container}>
        <SectionHeading
          index={STRUCTURE_STORY.index}
          eyebrow={STRUCTURE_STORY.eyebrow}
          title={STRUCTURE_STORY.title}
          subtitle={STRUCTURE_STORY.subtitle}
        />
        <ScrollReveal>
          {error ? (
            <div className={styles.realReplayLoading}>
              真实结构数据加载失败（{error}），请稍后刷新。
            </div>
          ) : !replay || !frame || !indicators || !viewport ? (
            <div className={styles.realReplayLoading}>
              正在加载真实结构图…
            </div>
          ) : (
            <RealStructureReplay
              symbol={replay.instrument.symbol}
              displayName={replay.instrument.name}
              bars={currentBars}
              indicators={indicators}
              viewport={viewport}
              frameIndex={player.frameIndex}
              frameCount={replay.frames.length}
              playing={player.playing}
              onPlay={player.play}
              onPause={player.pause}
              onReplay={player.replay}
              onPrevious={player.previous}
              onNext={player.next}
            />
          )}
        </ScrollReveal>
      </div>
    </section>
  )
}