// 真实结构回放图表工作区（V1.2）。
// 复用生产 StrategyChart 渲染真实 K 线 + canonical SMC 结构，不复制算法、不造第二套 Canvas。
// StrategyChart 用 lazy 分块加载，避免阻塞 Hero 首屏。
import { lazy, Suspense } from 'react'
import type { IndicatorResponse } from '@/api/endpoints'
import type { ChartViewport } from '@/components/chartViewport'
import type { ChartLayerVisibility } from '@/features/stock-research/stockResearchTypes'
import {
  STRUCTURE_STORY,
} from '../data/copy'
import styles from '../marketing.module.scss'

const StrategyChart = lazy(() => import('@/components/StrategyChart'))

// 只展示营销回放需要的图层：K线 + 成交量 + 结构。
// 这是显示 preset，不是算法复制。node/boll/macd/sqzmom/breakout/trend 营销不需要。
const REPLAY_LAYERS: ChartLayerVisibility = {
  trend: false,
  node: false,
  boll: false,
  volume: true,
  macd: false,
  sqzmom: false,
  breakout: false,
  smc: true,
}

export interface RealReplayFrameProps {
  symbol: string
  displayName: string
  bars: { time: string; open: number; high: number; low: number; close: number; volume: number }[]
  indicators: IndicatorResponse
  viewport: ChartViewport
  frameIndex: number
  frameCount: number
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onReplay: () => void
  onPrevious: () => void
  onNext: () => void
}

export default function RealStructureReplay(props: RealReplayFrameProps) {
  const {
    symbol,
    displayName,
    bars,
    indicators,
    viewport,
    frameIndex,
    frameCount,
    playing,
    onPlay,
    onPause,
    onReplay,
    onPrevious,
    onNext,
  } = props

  return (
    <figure className={styles.realReplayFrame} data-testid="marketing-real-structure-replay">
      <div className={styles.realReplayMeta}>
        <span>{STRUCTURE_STORY.instrumentLabel}</span>
        <span>{STRUCTURE_STORY.timeframeLabel}</span>
        <span>{STRUCTURE_STORY.dataLabel}</span>
      </div>

      <div className={styles.realReplayHeader}>
        <span className={styles.realReplayDate}>
          当前日期 {bars[frameIndex - 1]?.time ?? '—'}
        </span>
        <span className={styles.realReplayFrameIndex}>
          {frameIndex} / {frameCount}
        </span>
      </div>

      <div
        className={styles.realReplayProgressBg}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={frameCount}
        aria-valuenow={frameIndex}
      >
        <div
          className={styles.realReplayProgressFill}
          style={{ width: `${(frameIndex / Math.max(frameCount, 1)) * 100}%` }}
        />
      </div>

      <Suspense
        fallback={
          <div className={styles.realReplayLoading}>
            正在加载真实结构图…
          </div>
        }
      >
        <StrategyChart
          symbol={symbol}
          displayName={displayName}
          viewport={viewport}
          timeframe="1d"
          strategyId="watchlist_monitor"
          source="watchlist"
          height={430}
          bars={bars}
          indicators={indicators}
          layerVisibility={REPLAY_LAYERS}
        />
      </Suspense>

      <div className={styles.realReplayControls}>
        <button
          type="button"
          className={styles.realReplayBtn}
          onClick={onPrevious}
          aria-label="上一帧"
        >
          ←
        </button>
        {playing ? (
          <button
            type="button"
            className={styles.realReplayBtnPrimary}
            onClick={onPause}
          >
            {STRUCTURE_STORY.pauseLabel}
          </button>
        ) : (
          <button
            type="button"
            className={styles.realReplayBtnPrimary}
            onClick={onPlay}
          >
            {STRUCTURE_STORY.playLabel}
          </button>
        )}
        <button
          type="button"
          className={styles.realReplayBtn}
          onClick={onNext}
          aria-label="下一帧"
        >
          →
        </button>
        <button
          type="button"
          className={styles.realReplayBtnGhost}
          onClick={onReplay}
        >
          {STRUCTURE_STORY.replayLabel}
        </button>
      </div>

      <figcaption className={styles.realReplayCaption}>
        历史数据演示 · 使用盘迹真实结构计算代码（不构成投资建议）
      </figcaption>
    </figure>
  )
}