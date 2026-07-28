// [Round 2026-07-28-3] 第一金字塔统一快照面板
// 调用 GET /api/v1/stocks/{symbol}/first-pyramid，展示趋势→结构→动量→筹码共识四维状态。
// 双 variant 共用同一 React Query 缓存：
//   - compact：/market 右栏，紧凑摘要为主，事件最多3条
//   - detail：/stock 详情 Drawer，全宽，事件最多5条，显示日期/价格
// 严格禁止：
//   - 显示 algorithmVersion/inputHash/parameterHash
//   - 显示 Swing/Internal/up/down 字面
//   - 显示原始 volume 大整数（仅显示 ratio）
//   - 解析 statusText 推断多空或事件类型（必须读 continuousFactors 结构化字段）
//   - 内联 style 堆积（全部进入 CSS Module）
import { useFirstPyramid } from '@/hooks/useApi'
import { buildFirstPyramidVM, directionClass, directionLabel, volumeBadgeClass } from './firstPyramidViewModel'
import type { Direction, FirstPyramidVM, StructureEventVM } from './firstPyramidViewModel'
import styles from './FirstPyramidPanel.module.scss'

interface FirstPyramidPanelProps {
  symbol: string
  variant?: 'compact' | 'detail'
  className?: string
}

// ===== 内部展示组件（按 instruction.md 命名） =====

function PyramidHeader({ tradeDate }: { tradeDate: string }) {
  return (
    <div className={styles.header}>
      <span className={styles.title}>第一金字塔</span>
      <span className={styles.tradeDate}>{tradeDate}</span>
    </div>
  )
}

function PyramidSummaryStrip({ statusText }: { statusText: string }) {
  return (
    <div className={styles.summary} title={statusText}>
      {statusText}
    </div>
  )
}

/** compact 顶部 2x2 摘要网格：趋势｜结构 / 动量｜筹码 */
function SummaryGrid({ vm }: { vm: FirstPyramidVM }) {
  const cells: Array<{ label: string; dir: Direction; text: string }> = [
    { label: '趋势', dir: vm.trend.direction, text: directionLabel(vm.trend.direction) },
    { label: '结构', dir: vm.structure.swingDirection, text: directionLabel(vm.structure.swingDirection) },
    { label: '动量', dir: vm.momentum.direction, text: vm.momentum.squeezeOn ? '挤压' : '释放' },
    {
      label: '筹码',
      dir: vm.chipConsensus && vm.chipConsensus.available ? 1 : 0,
      text: vm.chipConsensus && vm.chipConsensus.available ? '可用' : '可选',
    },
  ]
  return (
    <div className={styles.summaryGrid}>
      {cells.map((c) => (
        <div key={c.label} className={styles.summaryCell}>
          <span className={styles.summaryLabel}>{c.label}</span>
          <span className={`${styles.summaryDir} ${styles[directionClass(c.dir)]}`}>
            {c.text}
          </span>
        </div>
      ))}
    </div>
  )
}

function VolumeWaterLevel({ vm }: { vm: FirstPyramidVM['volumeWaterLevel'] }) {
  if (!vm.ready) {
    return (
      <div className={`${styles.volumeBar} ${styles.na}`}>
        <span className={styles.volumeLabel}>量能水位</span>
        <span className={styles.volumeNa}>样本不足</span>
      </div>
    )
  }
  const pct20 = vm.percentile20 ?? 0
  const pct200 = vm.percentile200 ?? 0
  return (
    <div className={styles.volumeBar}>
      <span className={styles.volumeLabel}>量能水位</span>
      <div className={styles.volumeTrackGroup}>
        <div className={styles.volumeTrack}>
          <span className={styles.volumeTrackLabel}>20日</span>
          <div
            className={styles.volumeScale}
            style={{ '--vol-pct': `${Math.min(100, Math.max(0, pct20))}%` } as React.CSSProperties}
          >
            <div className={styles.volumeFill} />
          </div>
          <span className={styles.volumePct}>{pct20.toFixed(0)}</span>
          {vm.zscore20 !== null && (
            <span className={styles.volumeZscore}>z={vm.zscore20.toFixed(2)}</span>
          )}
        </div>
        <div className={styles.volumeTrack}>
          <span className={styles.volumeTrackLabel}>200日</span>
          <div
            className={styles.volumeScale}
            style={{ '--vol-pct': `${Math.min(100, Math.max(0, pct200))}%` } as React.CSSProperties}
          >
            <div className={`${styles.volumeFill} ${styles.long}`} />
          </div>
          <span className={styles.volumePct}>{pct200.toFixed(0)}</span>
          {vm.zscore200 !== null && (
            <span className={styles.volumeZscore}>z={vm.zscore200.toFixed(2)}</span>
          )}
        </div>
      </div>
      <span className={`${styles.badge} ${styles[volumeBadgeClass(vm.badge)]}`}>
        {vm.badge ?? '未知'}
      </span>
    </div>
  )
}

function TrendStateCard({ vm }: { vm: FirstPyramidVM['trend'] }) {
  return (
    <div className={`${styles.dimCard} ${styles.dimTrend}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>趋势</span>
        <span className={`${styles.dimBadge} ${vm.available ? styles.ok : styles.na}`}>
          {vm.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className={`${styles.summaryDir} ${styles[directionClass(vm.direction)]}`}>
        {directionLabel(vm.direction)}
      </div>
      {vm.continuousBars !== null && (
        <div className={styles.dimVolDetail}>
          <span>持续 {vm.continuousBars}根</span>
          {vm.currentVsPrevVolumeRatio !== null && (
            <span className={styles.volRatio}>
              量比 {vm.currentVsPrevVolumeRatio.toFixed(2)}x
            </span>
          )}
          {vm.freshnessLabel && (
            <span className={styles.freshness}>{vm.freshnessLabel}</span>
          )}
        </div>
      )}
    </div>
  )
}

function StructureStateCard({
  vm,
  variant,
}: {
  vm: FirstPyramidVM['structure']
  variant: 'compact' | 'detail'
}) {
  return (
    <div className={`${styles.dimCard} ${styles.dimStructure} ${variant === 'detail' ? styles.structureFull : ''}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>结构</span>
        <span className={`${styles.dimBadge} ${vm.available ? styles.ok : styles.na}`}>
          {vm.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className={styles.dimStructureDir}>
        <span className={styles.structureLabel}>
          主要结构：
          <span className={styles[directionClass(vm.swingDirection)]}>
            {directionLabel(vm.swingDirection)}
          </span>
        </span>
        <span className={styles.structureLabel}>
          短线结构：
          <span className={styles[directionClass(vm.internalDirection)]}>
            {directionLabel(vm.internalDirection)}
          </span>
        </span>
      </div>
      {vm.events.length > 0 && <StructureEventList events={vm.events} variant={variant} />}
    </div>
  )
}

function StructureEventList({
  events,
  variant,
}: {
  events: StructureEventVM[]
  variant: 'compact' | 'detail'
}) {
  return (
    <div className={styles.dimEvents}>
      {events.map((e, i) => {
        const parts: string[] = [e.typeLabel]
        if (e.directionLabel && e.directionLabel !== '—') parts.push(e.directionLabel)
        if (e.levelLabel) parts.push(e.levelLabel)
        if (e.freshnessLabel) parts.push(e.freshnessLabel)
        if (variant === 'detail' && e.occurredAt) parts.push(e.occurredAt)
        if (variant === 'detail' && e.price !== null) parts.push(`价 ${e.price}`)
        return (
          <div className={styles.eventItem} key={`${e.typeLabel}-${i}`}>
            <span className={styles.eventText}>{parts.join(' · ')}</span>
            {e.volumeBadge && (
              <span className={`${styles.badge} ${styles[volumeBadgeClass(e.volumeBadge)]}`}>
                {e.volumeBadge}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function MomentumStateCard({ vm }: { vm: FirstPyramidVM['momentum'] }) {
  return (
    <div className={`${styles.dimCard} ${styles.dimMomentum}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>动量</span>
        <span className={`${styles.dimBadge} ${vm.available ? styles.ok : styles.na}`}>
          {vm.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className={`${styles.summaryDir} ${styles[directionClass(vm.direction)]}`}>
        {vm.squeezeOn ? '挤压中' : '已释放'}
      </div>
      {vm.releaseVsSqueezeRatio !== null && (
        <div className={styles.dimVolDetail}>
          <span className={styles.volRatio}>
            释放/挤压 {vm.releaseVsSqueezeRatio.toFixed(2)}x
          </span>
          {vm.volDivergence && (
            <span className={`${styles.badge} ${styles[volumeBadgeClass(
              vm.volDivergence === '放量释放' ? '放量' : '缩量',
            )]}`}>
              {vm.volDivergence}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function ChipConsensusCard({ vm }: { vm: FirstPyramidVM['chipConsensus'] | null }) {
  if (!vm) {
    return (
      <div className={`${styles.dimCard} ${styles.dimChip} ${styles.optional}`}>
        <div className={styles.dimHeader}>
          <span className={styles.dimName}>筹码共识</span>
          <span className={`${styles.dimBadge} ${styles.na}`}>可选维度</span>
        </div>
        <div className={styles.dimEmpty}>暂无有效筹码峰</div>
      </div>
    )
  }
  return (
    <div className={`${styles.dimCard} ${styles.dimChip} ${styles.optional}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>筹码共识</span>
        <span className={`${styles.dimBadge} ${vm.available ? styles.ok : styles.na}`}>
          {vm.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className={styles.dimStatus} title={vm.statusText}>
        {vm.statusText}
      </div>
    </div>
  )
}

// ===== 主组件 =====

export function FirstPyramidPanel({ symbol, variant = 'detail', className }: FirstPyramidPanelProps) {
  const { data, isLoading, error } = useFirstPyramid(symbol)

  // 切换股票时：只有 data.symbol === symbol 才显示，禁止短暂显示上一只股票状态
  if (data && data.symbol !== symbol) {
    return (
      <div className={`${styles.loading} ${className ?? ''}`}>
        <div className={styles.title}>第一金字塔</div>
        <div className={styles.status}>加载中...</div>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className={`${styles.loading} ${className ?? ''}`}>
        <div className={styles.title}>第一金字塔</div>
        <div className={styles.status}>加载中...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className={`${styles.error} ${className ?? ''}`}>
        <div className={styles.title}>第一金字塔</div>
        <div className={styles.status}>
          加载失败：{error instanceof Error ? error.message : '未知错误'}
        </div>
      </div>
    )
  }

  if (!data) return null

  const vm = buildFirstPyramidVM(data, variant)
  const variantClass = variant === 'compact' ? styles.compact : styles.detail

  return (
    <div className={`${styles.panel} ${variantClass} ${className ?? ''}`}>
      <PyramidHeader tradeDate={vm.tradeDate} />
      <PyramidSummaryStrip statusText={vm.statusText} />
      {variant === 'compact' && <SummaryGrid vm={vm} />}
      <VolumeWaterLevel vm={vm.volumeWaterLevel} />
      <div className={styles.dimensions}>
        <TrendStateCard vm={vm.trend} />
        <StructureStateCard vm={vm.structure} variant={variant} />
        <MomentumStateCard vm={vm.momentum} />
        <ChipConsensusCard vm={vm.chipConsensus} />
      </div>
    </div>
  )
}
