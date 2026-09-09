import styles from '../marketing.module.scss'

type Props = {
  playing: boolean
  onPlay: () => void
  onPause: () => void
  onPrevious: () => void
  onNext: () => void
  onReplay: () => void

  /** 用于 aria-label：播放结构演示 / 播放筹码共识演示 */
  playLabel: string
  pauseLabel: string

  frame: number
  frameCount: number
}

export function StoryControls({
  playing,
  onPlay,
  onPause,
  onPrevious,
  onNext,
  onReplay,
  playLabel,
  pauseLabel,
  frame,
  frameCount,
}: Props) {
  return (
    <div className={styles.controls} data-testid="story-controls">
      <button type="button" className={styles.controlBtn} onClick={onPrevious}>
        上一步
      </button>
      <button
        type="button"
        className={styles.controlBtn}
        onClick={playing ? onPause : onPlay}
        aria-label={playing ? pauseLabel : playLabel}
      >
        {playing ? '暂停' : '播放'}
      </button>
      <button type="button" className={styles.controlBtn} onClick={onNext}>
        下一步
      </button>
      <button type="button" className={styles.controlBtn} onClick={onReplay}>
        重新播放
      </button>
      <span className={styles.frameReadout}>
        {frame} / {frameCount}
      </span>
    </div>
  )
}
