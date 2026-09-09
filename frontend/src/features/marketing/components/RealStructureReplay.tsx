// 真实结构回放图表工作区（V1.3）。
// 复用生产 StrategyChart 渲染真实 K 线 + canonical SMC 结构，不复制算法、不造第二套 Canvas。
// V1.3 布局：结构导读（3 项）→ 左图右解（workspace）→ 关键节点播放控制。
// 进度以百分比展示，不再暴露内部 canonical frame 编号。
import { lazy, Suspense } from 'react'
import type { IndicatorResponse } from '@/api/endpoints'
import type { ChartViewport } from '@/components/chartViewport'
import type { ChartLayerVisibility } from '@/features/stock-research/stockResearchTypes'
import {
  STRUCTURE_STORY,
} from '../data/copy'
import type { StructureBeat } from '../data/structureReplayNarration'
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

export interface NarrationPanelContent {
  title: string
  meaning: string
  explanation: string
  watch: string
}

export interface RealReplayFrameProps {
  symbol: string
  displayName: string
  bars: { time: string; open: number; high: number; low: number; close: number; volume: number }[]
  indicators: IndicatorResponse
  viewport: ChartViewport
  progress: number
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onReplay: () => void
  guide: ReadonlyArray<{ label: string; meaning: string; desc: string }>
  narration: StructureBeat | undefined
  defaultNarration: NarrationPanelContent
  onPreviousKey: (() => void) | undefined
  onNextKey: (() => void) | undefined
}

const NARRATION_KIND_LABEL: Record<string, string> = {
  context: '观察中',
  battle: '博弈位置',
  continuation: '趋势延续',
  reversal: '反转信号',
}

export default function RealStructureReplay(props: RealReplayFrameProps) {
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
    guide,
    narration,
    defaultNarration,
    onPreviousKey,
    onNextKey,
  } = props

  const currentDate = bars.at(-1)?.time.slice(0, 10) ?? '—'
  const progressPercent = Math.round(progress * 100)
  const panel =
    narration ??
    ({ kind: 'context' as const, title: defaultNarration.title, meaning: defaultNarration.meaning, explanation: defaultNarration.explanation, watch: defaultNarration.watch } as const)

  return (
    <figure className={styles.realReplayFrame} data-testid="marketing-real-structure-replay">
      {/* 结构导读：用户先看到 3 个解释，再进入动画。 */}
      <div className={styles.structureGuide}>
        {guide.map((item) => (
          <div className={styles.structureGuideItem} key={item.label}>
            <span className={styles.structureGuideLabel}>{item.label}</span>
            <span className={styles.structureGuideMeaning}>{item.meaning}</span>
            <span className={styles.structureGuideDesc}>{item.desc}</span>
          </div>
        ))}
      </div>

      <div className={styles.realReplayWorkspace}>
        {/* 左：真实结构图。 */}
        <div className={styles.realReplayChartPanel}>
          <div className={styles.realReplayMeta}>
            <span>{STRUCTURE_STORY.instrumentLabel}</span>
            <span>{STRUCTURE_STORY.timeframeLabel}</span>
            <span>{STRUCTURE_STORY.dataLabel}</span>
          </div>

          <div className={styles.realReplayHeader}>
            <span className={styles.realReplayDate}>
              {STRUCTURE_STORY.currentDateLabel} {currentDate}
            </span>
            <span className={styles.realReplayProgressText}>
              {STRUCTURE_STORY.progressLabel} {progressPercent}%
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
              height={460}
              bars={bars}
              indicators={indicators}
              layerVisibility={REPLAY_LAYERS}
            />
          </Suspense>
        </div>

        {/* 右：动态解释面板。 */}
        <aside className={styles.structureNarration}>
          <span className={styles.structureNarrationEyebrow}>
            {STRUCTURE_STORY.narrationTitle}
          </span>
          <span className={styles.structureNarrationKind}>
            {NARRATION_KIND_LABEL[panel.kind] ?? '观察中'}
          </span>
          <h3 className={styles.structureNarrationTitle}>{panel.title}</h3>
          <p className={styles.structureNarrationMeaning}>{panel.meaning}</p>
          <p className={styles.structureNarrationText}>{panel.explanation}</p>
          <p className={styles.structureNarrationWatch}>{panel.watch}</p>
        </aside>
      </div>

      <div className={styles.realReplayControls}>
        <button
          type="button"
          className={styles.realReplayBtn}
          onClick={onPreviousKey}
          disabled={!onPreviousKey}
          aria-label={STRUCTURE_STORY.previousKeyLabel}
        >
          {STRUCTURE_STORY.previousKeyLabel}
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
          onClick={onNextKey}
          disabled={!onNextKey}
          aria-label={STRUCTURE_STORY.nextKeyLabel}
        >
          {STRUCTURE_STORY.nextKeyLabel}
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