// [ReviewHeader] - 描述: 复盘固定顶部（PRD §14.1）
// 展示：交易日与前后交易日 / Review发布状态 / Core/Board Run /
//      股票与板块覆盖率 / 算法版本、筛选器版本、历史基线 / 数据质量入口
// 顶部不得显示 AI 自由生成的市场结论
import type { ReviewChipCoverage, ReviewOverview } from './types'
import styles from './review.module.scss'

const RUN_STATUS_META: Record<string, { label: string; cls: string }> = {
  published: { label: '已发布', cls: 'chipSuccess' },
  computing: { label: '计算中', cls: 'chipInfo' },
  created: { label: '已创建', cls: 'chipDefault' },
  partial: { label: '部分完成', cls: 'chipWarning' },
  signals_ready: { label: '信号就绪', cls: 'chipInfo' },
  completed_with_errors: { label: '完成但有错误', cls: 'chipWarning' },
  failed: { label: '失败', cls: 'chipDanger' },
  cancelled: { label: '已取消', cls: 'chipDefault' },
  // [AC-TERMINAL-01 2026-08-04] 终态如实呈现，不回落原始英文 key
  interrupted: { label: '已中断', cls: 'chipWarning' },
  partial_success: { label: '部分成功', cls: 'chipWarning' },
}

// [QM-63 2026-08-04] 降级原因中文说明。
// 未收录的 code 原样展示，不猜测、不静默丢弃。
const DEGRADED_REASON_LABEL: Record<string, string> = {
  CHIP_UNAVAILABLE: '筹码数据不可用（core-only 降级）',
  CHIP_PARTIAL: '筹码数据部分成功（覆盖不完整）',
  AUCTION_UNAVAILABLE: '竞价数据不可用',
  AUCTION_FAILED: '竞价计算失败',
}

function degradedReasonText(code: string): string {
  return DEGRADED_REASON_LABEL[code] ?? code
}

// [P0 2026-08-04] chip 覆盖率悬浮说明：真实覆盖/成功/缺失明细
function chipCoverageTitle(cov: ReviewChipCoverage): string {
  return (
    `chip 覆盖率 ${Math.round((cov.coverage ?? 0) * 100)}%` +
    `（成功 ${cov.succeededCount}/${cov.expectedCount ?? 0}，缺失 ${cov.missingCount}）`
  )
}

function CoverageItem({ label, ratio }: { label: string; ratio: number | null | undefined }) {
  const pct = ratio !== null && ratio !== undefined ? Math.round(ratio * 100) : null
  return (
    <span className={styles.coverageBar} title={`${label} 覆盖率`}>
      <span className={styles.metaLabel}>{label}</span>
      {pct !== null ? (
        <>
          <span className={styles.coverageTrack}>
            <span
              className={styles.coverageFill}
              style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
            />
          </span>
          <span className={styles.metaValue}>{pct}%</span>
        </>
      ) : (
        <span className={styles.metricUnavailable}>-</span>
      )}
    </span>
  )
}

export interface ReviewHeaderProps {
  overview: ReviewOverview | undefined
  tradeDate: string
  /** 可用复盘交易日（降序），用于前后切换 */
  availableDates: string[]
  onDateChange: (date: string) => void
  onOpenDataQuality?: () => void
}

export default function ReviewHeader({
  overview,
  tradeDate,
  availableDates,
  onDateChange,
  onOpenDataQuality,
}: ReviewHeaderProps) {
  // availableDates 为降序；当前日期索引
  const idx = availableDates.indexOf(tradeDate)
  // 降序中：前一个日期（更早）= idx+1；后一个日期（更新）= idx-1
  const prevDate = idx >= 0 && idx + 1 < availableDates.length ? availableDates[idx + 1] : null
  const nextDate = idx > 0 ? availableDates[idx - 1] : null

  const status = overview?.status ?? 'unknown'
  const statusMeta = RUN_STATUS_META[status] ?? { label: status, cls: 'chipDefault' }

  return (
    <header className={styles.header}>
      <div className={styles.headerTop}>
        <div className={styles.dateNav}>
          <button
            type="button"
            className={styles.dateBtn}
            disabled={!prevDate}
            onClick={() => prevDate && onDateChange(prevDate)}
            aria-label="前一交易日"
          >
            ‹
          </button>
          <span className={styles.dateText}>{tradeDate}</span>
          <button
            type="button"
            className={styles.dateBtn}
            disabled={!nextDate}
            onClick={() => nextDate && onDateChange(nextDate)}
            aria-label="后一交易日"
          >
            ›
          </button>
        </div>
        <span className={`${styles.chip} ${styles[statusMeta.cls]}`}>{statusMeta.label}</span>
        {overview && (
          <div className={styles.headerMeta}>
            <span className={styles.metaItem}>
              <span className={styles.metaLabel}>Core Run:</span>
              <span className={styles.metaValue}>{overview.sourceCoreRunId.slice(0, 8)}</span>
            </span>
            <span className={styles.metaItem}>
              <span className={styles.metaLabel}>Board Run:</span>
              <span className={styles.metaValue}>{overview.sourceBoardRunId.slice(0, 8)}</span>
            </span>
            {/* [P0 2026-08-04] chip 覆盖率：chip 无独立 run 记录，sourceChipRunId
                恒为 null，不得把 core run id 误称 Chip Run。展示真实覆盖率。 */}
            <span className={styles.metaItem}>
              <span className={styles.metaLabel}>Chip:</span>
              {overview.chipCoverage && overview.chipCoverage.coverage !== null ? (
                <span
                  className={styles.metaValue}
                  title={chipCoverageTitle(overview.chipCoverage)}
                >
                  {Math.round(overview.chipCoverage.coverage * 100)}%
                </span>
              ) : (
                <span
                  className={styles.metricUnavailable}
                  title="chip 共识不可用，本次复盘降级为 core-only"
                >
                  不可用
                </span>
              )}
            </span>
          </div>
        )}
      </div>
      {/* [QM-63] 降级横幅：有降级必须显式解释原因，禁止静默降级 */}
      {overview && overview.degradedReasons.length > 0 && (
        <div className={styles.headerBottom} role="status" aria-live="polite">
          <span className={`${styles.chip} ${styles.chipWarning}`}>数据降级</span>
          {overview.degradedReasons.map((code) => (
            <span key={code} className={styles.metaItem} title={code}>
              <span className={styles.metaValue}>{degradedReasonText(code)}</span>
            </span>
          ))}
        </div>
      )}
      {overview && (
        <div className={styles.headerBottom}>
          <CoverageItem label="全市场" ratio={overview.coverage.market} />
          <CoverageItem label="指数" ratio={overview.coverage.indices} />
          <CoverageItem label="风格" ratio={overview.coverage.styles} />
          <CoverageItem label="一级行业" ratio={overview.coverage.industryL1} />
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>信号:</span>
            <span className={styles.metaValue}>{overview.signalCount}</span>
            <span className={styles.metaLabel}>（新增 {overview.signalSummary.new}）</span>
          </span>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>算法:</span>
            <span className={styles.metaValue}>{overview.algorithmVersion}</span>
          </span>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>筛选器:</span>
            <span className={styles.metaValue}>{overview.filterVersion}</span>
          </span>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>基线:</span>
            <span className={styles.metaValue}>{overview.baselineWindow}日</span>
          </span>
          {onOpenDataQuality && (
            <button type="button" className={styles.btn} onClick={onOpenDataQuality}>
              数据质量
            </button>
          )}
        </div>
      )}
    </header>
  )
}
