// [ReviewStageNav] - 描述: 五阶段导航（PRD §14.2；PRD75 §3 竞价回流降级为 auxiliary）
// 1.市场扫描 2.筛选发现 3.板块归因 4.个股验证 5.追踪复核
// `auction`（竞价回流）不再是 Review 第六 formal stage，由 AuctionBackflowPanel 作为 auxiliary entry 渲染。
// 阶段共享同一上下文；切换阶段时更新 URL
import type { ReviewStage } from './types'
import { REVIEW_FORMAL_STAGES } from './urlState'
import styles from './review.module.scss'

export interface ReviewStageNavProps {
  stage: ReviewStage
  onChange: (stage: ReviewStage) => void
}

const STAGE_LABELS: Record<ReviewStage, string> = {
  scan: '市场扫描',
  signals: '筛选发现',
  attribution: '板块归因',
  validation: '个股验证',
  tracking: '追踪复核',
  auction: '竞价回流',
}

const STAGES: Array<{ value: ReviewStage; label: string }> = REVIEW_FORMAL_STAGES.map(
  (value) => ({ value, label: STAGE_LABELS[value] }),
)

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
