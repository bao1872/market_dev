// ChipConsensusStory（V1.4）— 真实筹码共识回放。
// - 数据仍从 MARKETING_MEDIA.chipConsensusReplay（静态 frozen JSON）same-origin fetch。
// - 播放复用 useSmoothMarketReplay（durationMs=42_000，M14，不复制 hook）。
// - 解释层复用生产 node_cluster DTO → buildChipConsensusBeats，
//   用数字化区域重叠（forming/priceLead/migration/retest）动态讲解，不用固定价格阈值。
// - 复用 findCanonicalFrameIndex（无未来：只取 endIndex <= 当前可见位置的最大帧）。
import { useEffect, useMemo, useState } from 'react'
import { createDefaultViewport } from '@/components/chartViewport'
import type { IndicatorResponse } from '@/api/endpoints'
import SectionHeading from '../components/SectionHeading'
import ScrollReveal from '../components/ScrollReveal'
import RealChipConsensusReplay from '../components/RealChipConsensusReplay'
import type { ChipMetrics } from '../components/RealChipConsensusReplay'
import { useInViewport } from '../hooks/useInViewport'
import { useSmoothMarketReplay } from '../hooks/useSmoothMarketReplay'
import {
  buildChipConsensusBeats,
  classifyPricePosition,
  CHIP_BEAT_KIND_LABEL,
} from '../data/chipConsensusNarration'
import type { ChipConsensusBeat } from '../data/chipConsensusNarration'
import { findCanonicalFrameIndex } from '../data/replayFrameUtils'
import { MARKETING_MEDIA, CHIP_CONSENSUS_STORY } from '../data/copy'
import type { MarketingChipConsensusReplay } from '../data/chipConsensusReplayTypes'
import styles from '../marketing.module.scss'

// 近岸蛋白真实回放：约42秒，64 个真实 node snapshot（250 根日线）。

// 图表只需该筹码共识图层；生产 node DTO 与 IndicatorResponse['data'] 泛型不同构，
// 在数据边界做一次窄化 cast（同 RealStructureReplay 的做法）。
function buildChipIndicators(
  replay: MarketingChipConsensusReplay,
  frame: MarketingChipConsensusReplay['frames'][number],
): IndicatorResponse {
  return {
    layers: [replay.nodeLayer],
    data: { node_cluster: frame.node } as IndicatorResponse['data'],
    errors: {},
    timeframe: replay.timeframe,
  }
}

const DEFAULT_NARRATION = {
  title: '正在观察成交重心',
  meaning: '市场成交从哪里集中往哪里移动',
  explanation:
    '前一小段K线用于让筹码共识计算热身，随后主要成交密集区会随价格与成交逐步形成、脱离、迁移。',
  watch: '继续播放，观察成交重心怎样一点一点移动。',
}

// 图表/轨迹用视觉 token（与 marketing 视觉体系一致，不硬编码新色板）。
const TRACK_COLORS = {
  track: '#00F6C2',
  cursor: '#00F6C2',
  bg: '#0A0F14',
  panel: '#161F29',
  border: '#1d2832',
  text: '#F2F6F8',
  textDim: '#657281',
}

export default function ChipConsensusStory() {
  const { ref, inView } = useInViewport<HTMLDivElement>(0.05)
  const [replay, setReplay] = useState<MarketingChipConsensusReplay | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!inView || replay || error) return
    let cancelled = false
    fetch(MARKETING_MEDIA.chipConsensusReplay)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json() as Promise<MarketingChipConsensusReplay>
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
    if (!replay) return [] as ChipConsensusBeat[]
    return buildChipConsensusBeats(replay)
  }, [replay])

  const firstEndIndex = replay?.frames[0]?.endIndex ?? 0
  const barCount = replay?.bars.length ?? 0

  const smooth = useSmoothMarketReplay({
    startEndIndex: firstEndIndex,
    endEndIndex: barCount,
    enabled: inView && !!replay,
    durationMs: 42_000,
  })

  const canonicalFrameIndex = useMemo(
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
    () => (replay && frame ? buildChipIndicators(replay, frame) : undefined),
    [replay, frame],
  )

  const viewport = useMemo(() => {
    if (!currentBars.length) return undefined
    return createDefaultViewport(currentBars.length, 180)
  }, [currentBars.length])

  // 顶部指标条（M17）：从生产 node DTO + 当前帧提取，区域关系语义。
  const metrics = useMemo<ChipMetrics>(() => {
    if (!frame) {
      return {
        currentPrice: null,
        consensusPrice: null,
        consensusLow: null,
        consensusHigh: null,
        position: 'none',
      }
    }
    const s = frame.summary
    const pocRegion =
      s.consensusLow != null && s.consensusHigh != null
        ? { low: s.consensusLow, mid: s.consensusMid ?? s.primaryConsensusPrice ?? 0, high: s.consensusHigh }
        : null
    const currentPrice = frame.node.state?.current_price ?? null
    const position =
      currentPrice != null && pocRegion
        ? classifyPricePosition(currentPrice, pocRegion)
        : 'none'
    return {
      currentPrice,
      consensusPrice: s.primaryConsensusPrice,
      consensusLow: s.consensusLow,
      consensusHigh: s.consensusHigh,
      position,
    }
  }, [frame])

  // 共识迁移轨迹（M19）：只采真实 POC（primaryConsensusPrice），离散 step。
  const trackPoints = useMemo(
    () =>
      (replay?.frames ?? []).map((f) => ({
        endIndex: f.endIndex,
        endTime: f.endTime,
        price: f.summary.primaryConsensusPrice,
      })),
    [replay],
  )

  // 当前叙事：取 atEndIndex <= 当前可见位置的最后一个 beat；无则用默认说明。
  const activeBeat = useMemo(() => {
    if (!beats.length) return undefined
    let best: ChipConsensusBeat | undefined
    for (const b of beats) {
      if (b.atEndIndex <= smooth.visibleEndIndex) best = b
    }
    return best
  }, [beats, smooth.visibleEndIndex])

  const narration = activeBeat
    ? { ...activeBeat, kindLabel: CHIP_BEAT_KIND_LABEL[activeBeat.kind] }
    : undefined

  const prevBeat = useMemo(() => {
    if (!activeBeat) return undefined
    let target: ChipConsensusBeat | undefined
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
      id="chip-consensus-story"
      data-testid="marketing-chip-consensus-story"
      ref={ref}
    >
      <div className={styles.container}>
        <SectionHeading
          index={CHIP_CONSENSUS_STORY.index}
          eyebrow={CHIP_CONSENSUS_STORY.eyebrow}
          title={CHIP_CONSENSUS_STORY.title}
          subtitle={CHIP_CONSENSUS_STORY.subtitle}
        />
        <ScrollReveal>
          {error ? (
            <div className={styles.realReplayLoading}>
              真实筹码共识数据加载失败（{error}），请稍后刷新。
            </div>
          ) : !replay || !frame || !indicators || !viewport ? (
            <div className={styles.realReplayLoading}>
              正在加载真实筹码共识图…
            </div>
          ) : (
            <RealChipConsensusReplay
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
              metrics={metrics}
              narration={narration}
              defaultNarration={DEFAULT_NARRATION}
              trackPoints={trackPoints}
              startEndIndex={firstEndIndex}
              endEndIndex={barCount}
              visibleEndIndex={smooth.visibleEndIndex}
              trackColors={TRACK_COLORS}
              onPreviousKey={prevBeat ? () => smooth.seekToEndIndex(prevBeat.atEndIndex) : undefined}
              onNextKey={nextBeat ? () => smooth.seekToEndIndex(nextBeat.atEndIndex) : undefined}
            />
          )}
        </ScrollReveal>
      </div>
    </section>
  )
}