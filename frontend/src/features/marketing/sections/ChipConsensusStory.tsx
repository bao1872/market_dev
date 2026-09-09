// ChipConsensusStory（Full Alignment V1 视觉升级）：
// 与 StructureStory 同一视觉体系，但左右镜像（避免整页重复）：
//   左：K线 + 成交分布（VolumeProfile），成交密集价以品牌实色高亮 + 动态细线标出
//   右：竖向 timeline（4 阶段）
// 逻辑保持 deterministic 教学剧本不变。
import { useMemo } from 'react'
import { KlineStoryChart } from '../components/KlineStoryChart'
import SectionHeading from '../components/SectionHeading'
import ScrollReveal from '../components/ScrollReveal'
import { StoryControls } from '../components/StoryControls'
import { useInViewport } from '../hooks/useInViewport'
import { useStoryPlayer } from '../hooks/useStoryPlayer'
import {
  CHIP_CANDLES,
  CHIP_STAGES,
  buildTeachingVolumeProfile,
  getPrimaryConsensusPrice,
} from '../demo/chipConsensusStory'
import { CHIP_CONSENSUS_STORY } from '../data/copy'
import { VolumeProfile } from './VolumeProfile'
import styles from '../marketing.module.scss'

const STAGE_ENDS = CHIP_STAGES.map((stage) => stage.endFrame)
const CHART_HEIGHT = 300

export default function ChipConsensusStory() {
  const { ref, inView } = useInViewport<HTMLDivElement>()

  const player = useStoryPlayer({
    frameCount: CHIP_CANDLES.length,
    stageEnds: STAGE_ENDS,
    enabled: inView,
  })

  const stage = CHIP_STAGES[player.stageIndex]

  const profile = useMemo(
    () => buildTeachingVolumeProfile(CHIP_CANDLES.slice(0, player.frame)),
    [player.frame],
  )

  const consensusPrice = getPrimaryConsensusPrice(profile)

  const initialConsensusPrice = useMemo(
    () => getPrimaryConsensusPrice(buildTeachingVolumeProfile(CHIP_CANDLES.slice(0, STAGE_ENDS[0]))),
    [],
  )

  const minPrice = Math.min(...CHIP_CANDLES.map((bar) => bar.low))
  const maxPrice = Math.max(...CHIP_CANDLES.map((bar) => bar.high))

  const format = (value: number | null) => (value === null ? '—' : value.toFixed(2))

  return (
    <section
      className={styles.section}
      id="chip-consensus-story"
      data-testid="marketing-chip-consensus-story"
    >
      <div className={styles.container}>
        <SectionHeading
          index={CHIP_CONSENSUS_STORY.index}
          eyebrow={CHIP_CONSENSUS_STORY.eyebrow}
          title={CHIP_CONSENSUS_STORY.title}
          subtitle={CHIP_CONSENSUS_STORY.subtitle}
        />
        <ScrollReveal>
          <div className={styles.chipStoryGrid} ref={ref}>
            {/* 左：K线 + 成交分布 */}
            <div className={styles.storyVisualSolo}>
              <div className={styles.chipProfileHead}>
                <span className={styles.consensusLabel}>
                  {CHIP_CONSENSUS_STORY.consensusLabel}
                </span>
                <span className={styles.consensusValue}>{format(consensusPrice)}</span>
                <span className={styles.consensusFrom}>
                  起始 {format(initialConsensusPrice)} ↓ 当前 {format(consensusPrice)}
                </span>
              </div>
              {/* 动态细线：主要成交密集价（随 frame 变化） */}
              <div className={styles.consensusLine} aria-hidden="true">
                <span className={styles.consensusLineLabel}>
                  主要成交密集价 {format(consensusPrice)}
                </span>
              </div>
              <div className={styles.chipVisualMirror}>
                <div className={styles.storyChart}>
                  <KlineStoryChart
                    candles={CHIP_CANDLES}
                    frame={player.frame}
                    height={CHART_HEIGHT}
                  />
                </div>
                <div className={styles.profilePanel}>
                  <VolumeProfile
                    profile={profile}
                    minPrice={minPrice}
                    maxPrice={maxPrice}
                    height={CHART_HEIGHT}
                    highlightPrice={consensusPrice}
                  />
                </div>
              </div>
              <div className={styles.storyExplain}>
                <h3 className={styles.storyExplainTitle}>{stage.title}</h3>
                <p className={styles.storyExplainText}>{stage.explanation}</p>
              </div>
            </div>

            {/* 右：竖向 timeline */}
            <ol className={styles.structureTimeline}>
              {CHIP_STAGES.map((item, index) => {
                const active = index === player.stageIndex
                return (
                  <li
                    key={item.id}
                    className={active ? styles.structureTimelineActive : styles.structureTimelineStep}
                    aria-current={active ? 'step' : undefined}
                  >
                    <span className={styles.structureTimelineDot} aria-hidden="true" />
                    <div className={styles.structureTimelineBody}>
                      <span className={styles.structureTimelineIndex}>
                        {String(index + 1).padStart(2, '0')}
                      </span>
                      <span className={styles.structureTimelineTitle}>{item.title}</span>
                      <span className={styles.structureTimelineSummary}>{item.summary}</span>
                    </div>
                  </li>
                )
              })}
            </ol>
          </div>

          <StoryControls
            playing={player.playing}
            onPlay={player.play}
            onPause={player.pause}
            onPrevious={player.previousStage}
            onNext={player.nextStage}
            onReplay={player.replay}
            playLabel={CHIP_CONSENSUS_STORY.playLabel}
            pauseLabel={CHIP_CONSENSUS_STORY.pauseLabel}
            frame={player.frame}
            frameCount={CHIP_CANDLES.length}
          />
        </ScrollReveal>
      </div>
    </section>
  )
}
