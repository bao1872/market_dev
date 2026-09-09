import { KlineStoryChart } from '../components/KlineStoryChart'
import SectionHeading from '../components/SectionHeading'
import ScrollReveal from '../components/ScrollReveal'
import { StoryControls } from '../components/StoryControls'
import { useInViewport } from '../hooks/useInViewport'
import { useStoryPlayer } from '../hooks/useStoryPlayer'
import { STRUCTURE_CANDLES, STRUCTURE_EVENTS, STRUCTURE_STAGES } from '../demo/structureStory'
import { STRUCTURE_STORY } from '../data/copy'
import styles from '../marketing.module.scss'

const STAGE_ENDS = STRUCTURE_STAGES.map((stage) => stage.endFrame)

// 教学动画：deterministic 剧本，不接实时行情，不复制生产算法。
export default function StructureStory() {
  const { ref, inView } = useInViewport<HTMLDivElement>()

  const player = useStoryPlayer({
    frameCount: STRUCTURE_CANDLES.length,
    stageEnds: STAGE_ENDS,
    enabled: inView,
  })

  const stage = STRUCTURE_STAGES[player.stageIndex]
  const reachedEvents = STRUCTURE_EVENTS.filter((event) => event.frame <= player.frame)
  const latestEvent = reachedEvents[reachedEvents.length - 1]

  return (
    <section
      className={styles.section}
      id="structure-story"
      data-testid="marketing-structure-story"
    >
      <div className={styles.container}>
        <SectionHeading
          index={STRUCTURE_STORY.index}
          eyebrow={STRUCTURE_STORY.eyebrow}
          title={STRUCTURE_STORY.title}
          subtitle={STRUCTURE_STORY.subtitle}
        />
        <ScrollReveal>
          <div className={styles.storyGrid} ref={ref}>
            <ol className={styles.storyStages}>
              {STRUCTURE_STAGES.map((item, index) => {
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
              <KlineStoryChart
                candles={STRUCTURE_CANDLES}
                frame={player.frame}
                events={STRUCTURE_EVENTS}
              />
              <div className={styles.storyExplain}>
                <h3 className={styles.storyExplainTitle}>{stage.title}</h3>
                <p className={styles.storyExplainText}>{stage.explanation}</p>
                {latestEvent ? (
                  <span className={styles.eventTag}>
                    {STRUCTURE_STORY.eventLabel}：{latestEvent.label}
                  </span>
                ) : null}
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
            playLabel={STRUCTURE_STORY.playLabel}
            pauseLabel={STRUCTURE_STORY.pauseLabel}
            frame={player.frame}
            frameCount={STRUCTURE_CANDLES.length}
          />
        </ScrollReveal>
      </div>
    </section>
  )
}
