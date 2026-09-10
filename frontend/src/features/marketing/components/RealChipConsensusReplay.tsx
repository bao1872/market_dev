// 真实筹码共识回放图表工作区（V1）。
// 复用生产 StrategyChart 渲染真实 K 线 + canonical node_cluster（price_zone），
// 不复制算法、不造第二套 Canvas。布局与 RealStructureReplay 同家族但版式不同：
//   顶部 指标条（当前日期/当前价/主要成交密集价/位置关系）
//   → workspace（左图右解）
//   → 底部 共识迁移轨迹（真实 POC 离散 step track，M19 视觉高潮）
//   底部 关键节点播放控制（上一个/播放/下一个/重新播放；关键节点 = narrative beat）。
import { lazy, Suspense } from 'react'
import type { IndicatorResponse } from '@/api/endpoints'
import type { ChartViewport } from '@/components/chartViewport'
import type { ChartLayerVisibility } from '@/features/stock-research/stockResearchTypes'
import { CHIP_CONSENSUS_STORY } from '../data/copy'
import type { ChipConsensusBeat } from '../data/chipConsensusNarration'
import {
  CHIP_BEAT_KIND_LABEL,
  CHIP_CONSENSUS_GUIDE,
} from '../data/chipConsensusNarration'
import type { MarketingReplayBar } from '../data/structureReplayTypes'
import ConsensusMigrationTrack from './ConsensusMigrationTrack'
import styles from '../marketing.module.scss'

const StrategyChart = lazy(() => import('@/components/StrategyChart'))

// 只展示营销回放需要的图层：K线 + 成交量 + 筹码共识节点（price_zone）。
// 这是显示 preset，不是算法复制。trend/boll/macd/sqzmom/breakout/smc 营销不需要。
const CHIP_LAYERS: ChartLayerVisibility = {
  trend: false,
  node: true,
  boll: false,
  volume: true,
  macd: false,
  sqzmom: false,
  breakout: false,
  smc: false,
}

// 顶部指标跑马灯路由（M17：语义是区域关系，不是百分比）。
const POSITION_TEXT: Record<'above' | 'inside' | 'below', string> = {
  above: CHIP_CONSENSUS_STORY.positionAbove,
  inside: CHIP_CONSENSUS_STORY.positionInside,
  below: CHIP_CONSENSUS_STORY.positionBelow,
}

export interface ChipMetrics {
  currentPrice: number | null
  consensusPrice: number | null
  consensusLow: number | null
  consensusHigh: number | null
  position: 'above' | 'inside' | 'below' | 'none'
}

export interface RealChipConsensusFrameProps {
  symbol: string
  displayName: string
  bars: MarketingReplayBar[]
  indicators: IndicatorResponse
  viewport: ChartViewport
  progress: number
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onReplay: () => void
  metrics: ChipMetrics
  narration: (ChipConsensusBeat & { kindLabel: string }) | undefined
  defaultNarration: { title: string; meaning: string; explanation: string; watch: string }
  trackPoints: Array<{ endIndex: number; endTime: string; price: number | null }>
  startEndIndex: number
  endEndIndex: number
  visibleEndIndex: number
  trackColors: {
    track: string
    cursor: string
    bg: string
    panel: string
    border: string
    text: string
    textDim: string
  }
  onPreviousKey: (() => void) | undefined
  onNextKey: (() => void) | undefined
}

const DEFAULT_KIND_LABEL = '观察中'

function fmt(value: number | null): string {
  return value == null ? '—' : value.toFixed(2)
}

export default function RealChipConsensusReplay(props: RealChipConsensusFrameProps) {
  const {
    symbol,
    displayName,
    bars,
    indicators,
    viewport,
    progress,
    playing,
    onPlay,
    onPause,
    onReplay,
    metrics,
    narration,
    defaultNarration,
    trackPoints,
    startEndIndex,
    endEndIndex,
    visibleEndIndex,
    trackColors,
    onPreviousKey,
    onNextKey,
  } = props

  const currentDate = bars.at(-1)?.time.slice(0, 10) ?? '—'
  const progressPercent = Math.round(progress * 100)

  const panel = narration
    ? narration
    : {
        kindLabel: DEFAULT_KIND_LABEL,
        atEndIndex: 0,
        kind: 'context',
        ...defaultNarration,
        eventTime: null,
      }

  return (
    <figure className={styles.realReplayFrame} data-testid="marketing-real-chip-consensus-replay">
      {/* 顶部导读（M16）：先建立语义，再进入动画。 */}
      <div className={styles.structureGuide}>
        {CHIP_CONSENSUS_GUIDE.map((item) => (
          <div className={styles.structureGuideItem} key={item.label}>
            <span className={styles.structureGuideLabel}>{item.label}</span>
            <span className={styles.structureGuideMeaning}>{item.meaning}</span>
            <span className={styles.structureGuideDesc}>{item.desc}</span>
          </div>
        ))}
      </div>

      {/* 顶部指标条（M17）。 */}
      <div className={styles.realChipMetricsStrip}>
        <div className={styles.realChipMetricsItem}>
          <span className={styles.realChipMetricsLabel}>
            {CHIP_CONSENSUS_STORY.currentDateLabel}
          </span>
          <span className={styles.realChipMetricsValue}>{currentDate}</span>
        </div>
        <div className={styles.realChipMetricsItem}>
          <span className={styles.realChipMetricsLabel}>
            {CHIP_CONSENSUS_STORY.currentPriceLabel}
          </span>
          <span className={styles.realChipMetricsValue}>{fmt(metrics.currentPrice)}</span>
        </div>
        <div className={styles.realChipMetricsItem}>
          <span className={styles.realChipMetricsLabel}>
            {CHIP_CONSENSUS_STORY.consensusPriceLabel}
          </span>
          <span className={styles.realChipMetricsValue}>
            {fmt(metrics.consensusPrice)}
          </span>
        </div>
        <div className={styles.realChipMetricsItem}>
          <span className={styles.realChipMetricsLabel}>
            {CHIP_CONSENSUS_STORY.positionLabel}
          </span>
          <span className={styles.realChipMetricsPosition}>
            {metrics.position === 'none'
              ? '—'
              : POSITION_TEXT[metrics.position]}
          </span>
        </div>
      </div>

      <div className={styles.realReplayMeta}>
        <span>{CHIP_CONSENSUS_STORY.instrumentLabel}</span>
        <span>{CHIP_CONSENSUS_STORY.timeframeLabel}</span>
        <span>{CHIP_CONSENSUS_STORY.dataLabel}</span>
      </div>

      <div className={styles.realReplayHeader}>
        <span className={styles.realReplayDate}>
          {CHIP_CONSENSUS_STORY.currentDateLabel} {currentDate}
        </span>
        <span className={styles.realReplayProgressText}>
          {CHIP_CONSENSUS_STORY.progressLabel} {progressPercent}%
        </span>
      </div>

      <div
        className={styles.realReplayProgressBg}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressPercent}
      >
        <div
          className={styles.realReplayProgressFill}
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      <div className={styles.realChipWorkspace}>
        {/* 左：真实图表。 */}
        <div className={styles.realReplayChartPanel}>
          <Suspense
            fallback={<div className={styles.realReplayLoading}>正在加载真实筹码共识图…</div>}
          >
            <StrategyChart
              symbol={symbol}
              displayName={displayName}
              viewport={viewport}
              timeframe="1d"
              strategyId="watchlist_monitor"
              source="watchlist"
              height={460}
              bars={bars}
              indicators={indicators}
              layerVisibility={CHIP_LAYERS}
            />
          </Suspense>
        </div>

        {/* 右：动态解释面板（M21）。 */}
        <aside className={styles.structureNarration}>
          <span className={styles.structureNarrationEyebrow}>
            {CHIP_CONSENSUS_STORY.narrationTitle}
          </span>
          <span className={styles.structureNarrationKind}>{panel.kindLabel}</span>
          <h3 className={styles.structureNarrationTitle}>{panel.title}</h3>
          <p className={styles.structureNarrationMeaning}>{panel.meaning}</p>
          <p className={styles.structureNarrationText}>{panel.explanation}</p>
          <p className={styles.structureNarrationWatch}>{panel.watch}</p>
        </aside>
      </div>

      {/* 共识迁移轨迹（M19）。 */}
      <div className={styles.realChipConsensusTrackWrap}>
        <span className={styles.realChipConsensusTrackLabel}>
          {CHIP_CONSENSUS_STORY.consensusTrackLabel}
        </span>
        <ConsensusMigrationTrack
          points={trackPoints}
          startEndIndex={startEndIndex}
          endEndIndex={endEndIndex}
          visibleEndIndex={visibleEndIndex}
          colors={trackColors}
        />
      </div>

      <div className={styles.realReplayControls}>
        <button
          type="button"
          className={styles.realReplayBtn}
          onClick={onPreviousKey}
          disabled={!onPreviousKey}
          aria-label={CHIP_CONSENSUS_STORY.previousKeyLabel}
        >
          {CHIP_CONSENSUS_STORY.previousKeyLabel}
        </button>
        {playing ? (
          <button
            type="button"
            className={styles.realReplayBtnPrimary}
            onClick={onPause}
          >
            {CHIP_CONSENSUS_STORY.pauseLabel}
          </button>
        ) : (
          <button
            type="button"
            className={styles.realReplayBtnPrimary}
            onClick={onPlay}
          >
            {CHIP_CONSENSUS_STORY.playLabel}
          </button>
        )}
        <button
          type="button"
          className={styles.realReplayBtn}
          onClick={onNextKey}
          disabled={!onNextKey}
          aria-label={CHIP_CONSENSUS_STORY.nextKeyLabel}
        >
          {CHIP_CONSENSUS_STORY.nextKeyLabel}
        </button>
        <button
          type="button"
          className={styles.realReplayBtnGhost}
          onClick={onReplay}
        >
          {CHIP_CONSENSUS_STORY.replayLabel}
        </button>
      </div>

      <figcaption className={styles.realReplayCaption}>
        {CHIP_CONSENSUS_STORY.caption}
      </figcaption>
    </figure>
  )
}

// 暴露供展示层复用；保持 CHIP_BEAT_KIND_LABEL 单一来源可被契约测试验证。
export { CHIP_BEAT_KIND_LABEL }