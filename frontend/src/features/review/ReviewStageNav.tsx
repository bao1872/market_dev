// [ReviewStageNav] - 描述: 五阶段导航（PRD §14.2）
// 1.市场扫描 2.筛选发现 3.板块归因 4.个股验证 5.追踪复核
// 阶段共享同一上下文；切换阶段时更新 URL
import type { ReviewStage } from './types'
import styles from './review.module.scss'

export interface ReviewStageNavProps {
  stage: ReviewStage
  onChange: (stage: ReviewStage) => void
}

const STAGES: Array<{ value: ReviewStage; label: string }> = [
  { value: 'scan', label: '市场扫描' },
  { value: 'signals', label: '筛选发现' },
  { value: 'attribution', label: '板块归因' },
  { value: 'validation', label: '个股验证' },
  { value: 'tracking', label: '追踪复核' },
]

export default function ReviewStageNav({ stage, onChange }: ReviewStageNavProps) {
  return (
    <nav className={styles.stageNav} aria-label="复盘阶段导航">
      {STAGES.map((s, i) => {
        const active = stage === s.value
        return (
          <button
            key={s.value}
            type="button"
            className={`${styles.stageTab} ${active ? styles.stageTabActive : ''}`}
            onClick={() => onChange(s.value)}
            aria-current={active ? 'step' : undefined}
          >
            <span className={`${styles.stageIdx} ${active ? styles.stageIdxActive : ''}`}>
              {i + 1}
            </span>
            {s.label}
          </button>
        )
      })}
    </nav>
  )
}
