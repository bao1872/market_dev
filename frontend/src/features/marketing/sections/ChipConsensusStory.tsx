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
const CHART_HEIGHT = 320

// 教学动画：deterministic 剧本，不接实时行情，不使用生产筹码算法。
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
          <div className={styles.storyGrid} ref={ref}>
            <ol className={styles.storyStages}>
              {CHIP_STAGES.map((item, index) => {
                const active = index === player.stageIndex
                return (
                  <li
                    key={item.id}
                    className={active ? styles.storyStageActive : styles.storyStage}
                    aria-current={active ? 'step' : undefined}
                  >
                    <span className={styles.storyStageIndex}>
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <span className={styles.storyStageTitle}>{item.title}</span>
                    <span className={styles.storyStageSummary}>{item.summary}</span>
                  </li>
                )
              })}
            </ol>

            <div className={styles.storyVisual}>
              <div className={styles.chipVisual}>
                <KlineStoryChart
                  candles={CHIP_CANDLES}
                  frame={player.frame}
                  height={CHART_HEIGHT}
                />
                <div className={styles.profilePanel}>
                  <div className={styles.consensusReadout}>
                    <span className={styles.consensusLabel}>
                      {CHIP_CONSENSUS_STORY.consensusLabel}
                    </span>
                    <span className={styles.consensusValue}>{format(consensusPrice)}</span>
                    <span className={styles.consensusFrom}>
                      起始 {format(initialConsensusPrice)} ↓ 当前 {format(consensusPrice)}
                    </span>
                  </div>
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
