// StructureStory（Full Alignment V1 视觉升级）：
// 逻辑保持 deterministic 教学剧本不变，仅改视觉：
//   左：竖向 timeline（4 阶段，active = 品牌绿 dot + 细线）
//   右：K线成为视觉主角（薄卡），事件在 chart 上方浮出
//   控件收敛为 ← 播放/暂停 → 重新播放，frame 1/24 为极小辅助信息
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
            {/* 左：竖向 timeline */}
            <ol className={styles.structureTimeline}>
              {STRUCTURE_STAGES.map((item, index) => {
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

            {/* 右：K线主角 + 事件浮出 + 说明 */}
            <div className={styles.storyVisualSolo}>
              {latestEvent ? (
                <div className={styles.storyEventFloat}>
                  {STRUCTURE_STORY.eventLabel}：{latestEvent.label}
                </div>
              ) : null}
              <div className={styles.storyChart}>
                <KlineStoryChart
                  candles={STRUCTURE_CANDLES}
                  frame={player.frame}
                  events={STRUCTURE_EVENTS}
                />
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
