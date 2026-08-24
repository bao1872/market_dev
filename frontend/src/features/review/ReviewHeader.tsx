// [ReviewHeader] - 描述: 复盘固定顶部（canonical Review，Slice D）
// 展示：交易日与前后交易日 / Review 发布状态 / Core Run /
//      overall coverageRatio / succeeded/expected/failed scope 计数 / 算法版本 / 历史基线 / 降级原因
// [R0 2026-08-24] Board runtime 已退役：sourceBoardRunId 当前恒为 null，仅历史数据可能非空；
//      前端不在主业务层显示「Board Run」，Legacy lineage 只放 diagnostics（见底部条件块）。
// [Slice D] 不再展示 retired Signal 语义：signalCount / signalSummary / “新增信号” /
//          旧 market/indices/styles 覆盖块 / Filter Version 产品概念。
// 顶部不得显示 AI 自由生成的市场结论；不可用值用 null 如实展示。
import type { ReviewOverview } from './types'
import { formatPercentNullable } from './reviewFormat'
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
  interrupted: { label: '已中断', cls: 'chipWarning' },
  partial_success: { label: '部分成功', cls: 'chipWarning' },
}

// 降级原因中文说明；未收录的 code 原样展示，不猜测、不静默丢弃。
const DEGRADED_REASON_LABEL: Record<string, string> = {
  AUCTION_UNAVAILABLE: '竞价数据不可用',
  AUCTION_FAILED: '竞价计算失败',
}

function degradedReasonText(code: string): string {
  return DEGRADED_REASON_LABEL[code] ?? code
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <span className={styles.metaItem}>
      <span className={styles.metaLabel}>{label}</span>
      <span className={styles.metaValue}>{value}</span>
    </span>
  )
}

export interface ReviewHeaderProps {
  overview: ReviewOverview | undefined
  tradeDate: string
  /** 可用复盘交易日（降序），用于前后切换 */
  availableDates: string[]
  onDateChange: (date: string) => void
}

export default function ReviewHeader({
  overview,
  tradeDate,
  availableDates,
  onDateChange,
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
            <MetaItem label="Core Run:" value={overview.sourceCoreRunId.slice(0, 8)} />
          </div>
        )}
      </div>
      {/* 降级横幅：有降级必须显式解释原因，禁止静默降级 */}
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
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>总体覆盖率:</span>
            <span className={styles.metaValue}>{formatPercentNullable(overview.coverageRatio)}</span>
          </span>
          <span className={styles.metaItem}>
            <span className={styles.metaLabel}>Scope:</span>
            <span className={styles.metaValue}>
              {overview.succeededScopeCount}/{overview.expectedScopeCount}
            </span>
          </span>
          {overview.failedScopeCount > 0 && (
            <span className={styles.metaItem}>
              <span className={styles.metaLabel}>失败:</span>
              <span className={styles.metaValue}>{overview.failedScopeCount}</span>
            </span>
          )}
          <MetaItem label="算法:" value={overview.algorithmVersion} />
          <MetaItem label="基线:" value={`${overview.baselineWindow}日`} />
        </div>
      )}
      {/* Legacy Board lineage（diagnostics，非主业务层）：当前 run 的 sourceBoardRunId 恒为 null；
          仅历史/回溯数据中非空时出现，不表示 Review 的上游或 runtime owner。 */}
      {overview && overview.sourceBoardRunId && (
        <div className={styles.headerBottom}>
          <MetaItem label="Legacy Board lineage:" value={overview.sourceBoardRunId.slice(0, 8)} />
        </div>
      )}
    </header>
  )
}
