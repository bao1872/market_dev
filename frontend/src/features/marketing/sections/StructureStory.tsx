// StructureStory（V1.3）— 真实结构回放「动画讲成故事」。
// - 数据仍从 MARKETING_MEDIA.structureReplay（静态 frozen JSON）same-origin fetch。
// - 播放改用 useSmoothMarketReplay：K 线平滑推进，canonical frame 只负责结构计算状态。
// - 解释层复用生产 DTO（events/order_blocks）→ buildStructureNarrativeBeats，
//   右侧面板随结构事件动态讲解（承接/压制、突破/跌破、转强/转弱）。
import { useEffect, useMemo, useState } from 'react'
import { createDefaultViewport } from '@/components/chartViewport'
import type { IndicatorResponse } from '@/api/endpoints'
import SectionHeading from '../components/SectionHeading'
import ScrollReveal from '../components/ScrollReveal'
import RealStructureReplay from '../components/RealStructureReplay'
import { useInViewport } from '../hooks/useInViewport'
import { useSmoothMarketReplay } from '../hooks/useSmoothMarketReplay'
import {
  buildStructureNarrativeBeats,
  findCanonicalFrameIndex,
  STRUCTURE_GUIDE,
} from '../data/structureReplayNarration'
import type { StructureBeat } from '../data/structureReplayNarration'
import { MARKETING_MEDIA, STRUCTURE_STORY } from '../data/copy'
import type { MarketingStructureReplay } from '../data/structureReplayTypes'
import styles from '../marketing.module.scss'

// 显示 preset 需要的指标视图。SMC DTO 由后端 canonical 直接产出，
// 与前端 IndicatorResponse['data'] 的泛型签名不同构，因此在数据边界做一次窄化 cast。
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

const DEFAULT_NARRATION = {
  title: 'K线正在逐步推进',
  meaning: '结构从真实数据长出',
  explanation:
    '前一小段K线用于让结构计算热身，随后承接/压制、突破/跌破、转强/转弱会随价格逐步被确认。',
  watch: '继续播放，观察结构一步一步怎样形成。',
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

  const beats = useMemo(() => {
    if (!replay) return [] as StructureBeat[]
    return buildStructureNarrativeBeats(replay)
  }, [replay])

  const firstEndIndex = replay?.frames[0]?.endIndex ?? 0
  const barCount = replay?.bars.length ?? 0

  const smooth = useSmoothMarketReplay({
    startEndIndex: firstEndIndex,
    endEndIndex: barCount,
    enabled: inView && !!replay,
  })

  const canonicalFrameIndex = useMemo(
    // 无未来：只取 endIndex <= 当前可见位置的最大 canonical 帧；
    // 首帧之前不存在可用结构帧（返回 -1），回放总是从首帧开始，故安全收敛到 0。
    () =>
      replay
        ? Math.max(0, findCanonicalFrameIndex(replay.frames, smooth.visibleEndIndex))
        : 0,
    [replay, smooth.visibleEndIndex],
  )

  const frame = replay?.frames[canonicalFrameIndex]

  const currentBars = useMemo(
    () => (replay ? replay.bars.slice(0, smooth.visibleEndIndex) : []),
    [replay, smooth.visibleEndIndex],
  )

  const indicators = useMemo(
    () => (replay && frame ? buildReplayIndicators(replay, frame) : undefined),
    [replay, frame],
  )

  const viewport = useMemo(() => {
    if (!currentBars.length) return undefined
    return createDefaultViewport(currentBars.length, 180)
  }, [currentBars.length])

  // 当前叙事：取 atEndIndex <= 当前可见位置的最后一个 beat；无则用默认说明。
  const activeBeat = useMemo(() => {
    let best: StructureBeat | undefined
    for (const b of beats) {
      if (b.atEndIndex <= smooth.visibleEndIndex) best = b
    }
    return best
  }, [beats, smooth.visibleEndIndex])

  const prevBeat = useMemo(() => {
    if (!activeBeat) return undefined
    let target: StructureBeat | undefined
    for (const b of beats) {
      if (b.atEndIndex < activeBeat.atEndIndex) target = b
    }
    return target
  }, [beats, activeBeat])

  const nextBeat = useMemo(() => {
    if (!activeBeat) return beats[0]
    return beats.find((b) => b.atEndIndex > activeBeat.atEndIndex)
  }, [beats, activeBeat])

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
              progress={smooth.progress}
              playing={smooth.playing}
              onPlay={smooth.play}
              onPause={smooth.pause}
              onReplay={smooth.replay}
              guide={STRUCTURE_GUIDE}
              narration={activeBeat}
              onPreviousKey={prevBeat ? () => smooth.seekToEndIndex(prevBeat.atEndIndex) : undefined}
              onNextKey={nextBeat ? () => smooth.seekToEndIndex(nextBeat.atEndIndex) : undefined}
              defaultNarration={DEFAULT_NARRATION}
            />
          )}
        </ScrollReveal>
      </div>
    </section>
  )
}