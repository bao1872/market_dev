// [Round 2026-07-28-4] 第一金字塔视觉重构
// compact: 单列纵向，StateRibbon + 量能水位 + 四维VisualCard
// detail: 两列布局，结构跨两列，事件最多5条
// 严格禁止：
//   - compact 渲染 PyramidSummaryStrip(statusText)
//   - 显示 DSA/Swing/Internal/CHoCH/Squeeze/dir_bars/Node 等内部英文
//   - 解析 statusText 推断多空或事件类型
//   - 内联 style 堆积（全部进入 CSS Module）
import { useFirstPyramid } from '@/hooks/useApi'
import type { ChipStatus, ChipStatusState } from '@/api/endpoints'
import { buildFirstPyramidVM, directionClass, directionLabel, volumeBadgeClass } from './firstPyramidViewModel'
import type { FirstPyramidVM, StructureEventVM } from './firstPyramidViewModel'
import styles from './FirstPyramidPanel.module.scss'

interface FirstPyramidPanelProps {
  symbol: string
  variant?: 'compact' | 'detail'
  className?: string
}

// ===== 内部展示组件 =====

function PyramidHeader({
  tradeDate,
  provenance,
}: {
  tradeDate: string
  provenance?: FirstPyramidVM['provenance']
}) {
  return (
    <div className={styles.header}>
      <span className={styles.title}>第一金字塔</span>
      <span className={styles.tradeDate}>{tradeDate}</span>
      {/* [QM-63] run 级溯源：批量 run 显示 run id + 计算时间；
          单股即时计算显式标注，不留空白让用户误以为数据缺失。 */}
      {provenance && (
        provenance.fromBatchRun ? (
          <span
            className={styles.tradeDate}
            title={[
              `source run: ${provenance.sourceRunId}`,
              `calculatedAt: ${provenance.calculatedAt}`,
              provenance.algorithmVersion ? `algo: ${provenance.algorithmVersion}` : null,
              provenance.inputHash ? `inputHash: ${provenance.inputHash}` : null,
              provenance.parameterHash ? `paramHash: ${provenance.parameterHash}` : null,
            ].filter(Boolean).join('\n')}
          >
            run {provenance.sourceRunId?.slice(0, 8)} · {provenance.calculatedAt}
          </span>
        ) : (
          <span className={styles.tradeDate} title="本快照为单股即时计算，非批量 run 产出">
            即时计算
          </span>
        )
      )}
    </div>
  )
}

/** compact 顶部一行4个状态标签（高度28px） */
function StateRibbon({ vm }: { vm: FirstPyramidVM }) {
  const trendLabel = directionLabel(vm.trend.direction)
  const structLabel = directionLabel(vm.structure.swingDirection)
  const momentumLabel = `${vm.momentum.squeezeOn ? '挤压' : '释放'}`
  const chipLabel = vm.chipConsensus?.positionLabel ?? (vm.chipConsensus ? '可用' : '可选')
  return (
    <div className={styles.stateRibbon}>
      <span className={`${styles.ribbonTag} ${styles[directionClass(vm.trend.direction)]}`} title={`趋势${trendLabel}`}>
        趋势·{trendLabel}
      </span>
      <span className={`${styles.ribbonTag} ${styles[directionClass(vm.structure.swingDirection)]}`} title={`主要结构${structLabel}`}>
        结构·{structLabel}
      </span>
      <span className={`${styles.ribbonTag} ${styles[directionClass(vm.momentum.direction)]}`} title={`动量${momentumLabel}`}>
        动量·{momentumLabel}
      </span>
      <span className={`${styles.ribbonTag} ${styles['dir-neutral']}`} title={`筹码${chipLabel}`}>
        筹码·{chipLabel}
      </span>
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
  return (
    <div className={styles.volumeBar}>
      <span className={styles.volumeLabel}>量能</span>
      <div className={styles.volumeTrackGroup}>
        <VolumeTrack label="20日" percentile={vm.percentile20} zscore={vm.zscore20} longTerm={false} />
        <VolumeTrack label="200日" percentile={vm.percentile200} zscore={vm.zscore200} longTerm={true} />
      </div>
      <span className={`${styles.badge} ${styles[volumeBadgeClass(vm.badge)]}`}>
        {vm.badge ?? '未知'}
      </span>
    </div>
  )
}

function VolumeTrack({
  label, percentile, zscore, longTerm,
}: {
  label: string
  percentile: number | null
  zscore: number | null
  longTerm: boolean
}) {
  if (percentile === null) {
    return (
      <div className={styles.volumeTrack}>
        <span className={styles.volumeTrackLabel}>{label}</span>
        <span className={styles.volumeNa}>样本不足</span>
      </div>
    )
  }
  const pctClamped = Math.min(100, Math.max(0, percentile))
  return (
    <div className={styles.volumeTrack}>
      <span className={styles.volumeTrackLabel}>{label}</span>
      <div
        className={styles.volumeScale}
        style={{ '--vol-pct': `${pctClamped}%` } as React.CSSProperties}
      >
        <div className={`${styles.volumeFill} ${longTerm ? styles.long : ''}`} />
      </div>
      <span className={styles.volumePct}>{percentile.toFixed(0)}%</span>
      {zscore !== null && (
        <span className={styles.volumeZscore}>z={zscore.toFixed(2)}</span>
      )}
    </div>
  )
}

/** 趋势卡：方向轨道 + 指标行 */
function TrendVisualCard({ vm, variant }: { vm: FirstPyramidVM['trend']; variant: 'compact' | 'detail' }) {
  // 方向轨道 marker 位置：偏空=0%, 未确认=50%, 偏多=100%
  const markerPct = vm.direction === 1 ? '100%' : vm.direction === -1 ? '0%' : '50%'
  // 量比轨道：0~2x 映射到 0~100%
  const volRatioPct = vm.segmentVolumeRatio !== null
    ? `${Math.min(100, Math.max(0, (vm.segmentVolumeRatio / 2) * 100))}%`
    : null
  return (
    <div className={`${styles.dimCard} ${styles.dimTrend}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>趋势</span>
        <span className={`${styles.dimArrow} ${styles[directionClass(vm.direction)]}`}>
          {vm.direction === 1 ? '↑' : vm.direction === -1 ? '↓' : '→'}
        </span>
      </div>
      {/* 方向轨道 */}
      <div className={styles.directionTrack}>
        <span className={styles.trackEnd}>偏空</span>
        <div className={styles.trackBar}>
          <div className={styles.trackFill} />
          <div className={styles.trackMarker} style={{ left: markerPct }} />
        </div>
        <span className={styles.trackEnd}>偏多</span>
      </div>
      {/* 指标行 */}
      <div className={styles.metricRow}>
        {vm.continuousBars !== null && (
          <span className={styles.metric}>持续 <b>{vm.continuousBars}</b>根</span>
        )}
        {vm.vwapDeviationPct !== null && (
          <span className={styles.metric}>距VWAP <b>{vm.vwapDeviationPct.toFixed(2)}%</b></span>
        )}
        {vm.trendStrength && (
          <span className={styles.metric}>{vm.trendStrength}</span>
        )}
      </div>
      {/* 量比轨道 */}
      {volRatioPct && vm.segmentVolumeRatio !== null && (
        <div className={styles.ratioTrack}>
          <span className={styles.ratioLabel}>段量比</span>
          <div className={styles.ratioBar}>
            <div className={styles.ratioFill} style={{ width: volRatioPct } as React.CSSProperties} />
          </div>
          <span className={styles.ratioValue}>{vm.segmentVolumeRatio.toFixed(2)}x</span>
        </div>
      )}
      {variant === 'detail' && vm.freshnessLabel && (
        <span className={styles.freshness}>{vm.freshnessLabel}</span>
      )}
    </div>
  )
}

/** 结构卡：两个状态块 + 事件chips */
function StructureVisualCard({
  vm, variant,
}: {
  vm: FirstPyramidVM['structure']
  variant: 'compact' | 'detail'
}) {
  return (
    <div className={`${styles.dimCard} ${styles.dimStructure} ${variant === 'detail' ? styles.structureFull : ''}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>结构</span>
      </div>
      {/* 两个独立状态块 */}
      <div className={styles.structBlocks}>
        <div className={styles.structBlock}>
          <span className={styles.structBlockLabel}>主要结构</span>
          <span className={`${styles.structBlockDir} ${styles[directionClass(vm.swingDirection)]}`}>
            {directionLabel(vm.swingDirection)}
          </span>
        </div>
        <div className={styles.structBlock}>
          <span className={styles.structBlockLabel}>短线结构</span>
          <span className={`${styles.structBlockDir} ${styles[directionClass(vm.internalDirection)]}`}>
            {directionLabel(vm.internalDirection)}
          </span>
        </div>
      </div>
      {/* 事件 chips */}
      {vm.events.length > 0 && (
        <div className={styles.eventTimeline}>
          {vm.events.map((e, i) => (
            <StructureEventChips key={`${e.typeLabel}-${i}`} event={e} variant={variant} />
          ))}
        </div>
      )}
    </div>
  )
}

/** 单个事件的独立 chips（不 join 为长字符串） */
function StructureEventChips({
  event, variant,
}: {
  event: StructureEventVM
  variant: 'compact' | 'detail'
}) {
  return (
    <div className={styles.eventRow}>
      <span className={styles.eventChip}>{event.typeLabel}</span>
      {event.levelLabel && (
        <span className={styles.eventChipSub}>{event.levelLabel}</span>
      )}
      {event.directionLabel && event.directionLabel !== '—' && (
        <span className={`${styles.eventChipSub} ${styles[directionClass(
          event.directionLabel === '上行' ? 1 : event.directionLabel === '下行' ? -1 : 0,
        )]}`}>
          {event.directionLabel}
        </span>
      )}
      {event.freshnessLabel && (
        <span className={styles.eventChipTime}>{event.freshnessLabel}</span>
      )}
      {variant === 'detail' && event.occurredAt && (
        <span className={styles.eventChipTime}>{event.occurredAt}</span>
      )}
      {variant === 'detail' && event.price !== null && (
        <span className={styles.eventChipTime}>价{event.price}</span>
      )}
      {event.volumeBadge && (
        <span className={`${styles.badge} ${styles[volumeBadgeClass(event.volumeBadge)]}`}>
          {event.volumeBadge}
        </span>
      )}
    </div>
  )
}

/** 动量卡：状态 + 方向 + BB位置轨道 + 变化 + 量价 */
function MomentumVisualCard({ vm, variant }: { vm: FirstPyramidVM['momentum']; variant: 'compact' | 'detail' }) {
  const bbPos = vm.bbPosition !== null ? Math.min(1, Math.max(0, vm.bbPosition)) : null
  const bbPct = bbPos !== null ? `${bbPos * 100}%` : null
  return (
    <div className={`${styles.dimCard} ${styles.dimMomentum}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>动量</span>
      </div>
      {/* 状态 + 方向 chips */}
      <div className={styles.momentumChips}>
        <span className={styles.eventChip}>
          {vm.squeezeOn ? '挤压中' : '已释放'}
        </span>
        <span className={`${styles.eventChipSub} ${styles[directionClass(vm.direction)]}`}>
          {directionLabel(vm.direction)}
        </span>
        {vm.momentumChangeLabel && (
          <span className={styles.eventChipSub}>{vm.momentumChangeLabel}</span>
        )}
      </div>
      {/* BB 位置轨道 */}
      {bbPct && (
        <div className={styles.bbTrack}>
          <span className={styles.bbLabel}>BB位置</span>
          <div className={styles.bbBar}>
            <span className={styles.bbMark} style={{ left: '0%' }}>下轨</span>
            <span className={styles.bbMark} style={{ left: '50%' }}>中轨</span>
            <span className={styles.bbMark} style={{ left: '100%' }}>上轨</span>
            <div className={styles.bbMarker} style={{ left: bbPct } as React.CSSProperties} />
          </div>
        </div>
      )}
      {/* 量价标签 */}
      {vm.volDivergence && (
        <span className={`${styles.badge} ${styles[volumeBadgeClass(
          vm.volDivergence === '放量释放' ? '放量' : '缩量',
        )]}`}>
          {vm.volDivergence}
        </span>
      )}
      {/* detail 模式才显示原始值 */}
      {variant === 'detail' && (
        <div className={styles.detailMetrics}>
          {vm.sqzmomVal !== null && (
            <span className={styles.metric}>动量值 <b>{vm.sqzmomVal.toFixed(4)}</b></span>
          )}
          {vm.bbWidth !== null && (
            <span className={styles.metric}>BB带宽 <b>{vm.bbWidth.toFixed(4)}</b></span>
          )}
          {vm.releaseVsSqueezeRatio !== null && (
            <span className={styles.metric}>释放/挤压 <b>{vm.releaseVsSqueezeRatio.toFixed(2)}x</b></span>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * [QM-63 2026-08-04] chip 七态展示文案（与后端 CHIP_STATUS_STATES 一一对应）。
 *
 * 每一态都必须有专属中文标签：缺标签会让 unavailable / interrupted / partial
 * 退化成「没有徽章」，用户无法区分「合法不可算」「被中断」「部分可用」。
 * ready 不显示徽章（正常态无需提示）。
 */
const CHIP_STATE_LABEL: Record<ChipStatusState, string | null> = {
  ready: null,
  pending: '筹码任务尚未执行',
  unavailable: '筹码本日不可算',
  failed: '筹码计算失败',
  interrupted: '筹码任务已中断',
  stale: '筹码结果已过期',
  partial: '筹码部分可用',
}

/**
 * [QM-63] chip 溯源与新鲜度行：source run / job / freshness / coverage。
 * 仅展示后端实际给出的字段，缺失即不渲染该项，不用占位值伪装已知。
 */
function ChipProvenance({ chipStatus }: { chipStatus: ChipStatus }) {
  const items: { label: string; value: string; title?: string }[] = []
  if (chipStatus.sourceRunId) {
    items.push({
      label: 'Chip Run',
      value: chipStatus.sourceRunId.slice(0, 8),
      title: chipStatus.sourceRunId,
    })
  }
  if (chipStatus.jobId) {
    items.push({
      label: 'Job',
      value: chipStatus.jobId.slice(0, 8),
      title: chipStatus.jobId,
    })
  }
  if (chipStatus.freshness !== null && chipStatus.freshness !== undefined) {
    items.push({
      label: '滞后',
      value: chipStatus.freshness === 0 ? '同日' : `${chipStatus.freshness}日`,
    })
  }
  if (chipStatus.coverage !== null && chipStatus.coverage !== undefined) {
    items.push({ label: '覆盖', value: `${(chipStatus.coverage * 100).toFixed(0)}%` })
  }
  if (chipStatus.computedAt) {
    items.push({ label: '计算于', value: chipStatus.computedAt })
  }
  if (items.length === 0) return null
  return (
    <div className={styles.metricRow}>
      {items.map((it) => (
        <span key={it.label} className={styles.metric} title={it.title}>
          {it.label} <b>{it.value}</b>
        </span>
      ))}
    </div>
  )
}

/** 筹码卡：POC位置轨道 + 距离% + 峰数量
 * [CHANGE-20260729-004 P0-2 + CHANGE-20260730-010 + QM-63] 当筹码不可用时：
 * - 显示 chipStatus.reasonText 真实原因（不再统一显示"暂无有效筹码峰"）
 * - 七态各有专属中文标签（见 CHIP_STATE_LABEL）
 * - state=unavailable + M15_BARS_INSUFFICIENT 显示 actualBars/requiredBars/fullQualityBars
 * - 展示 chip 溯源（source run / job / freshness / coverage / computedAt）
 */
function ChipVisualCard({
  vm,
  chipStatus,
}: {
  vm: FirstPyramidVM['chipConsensus'] | null
  chipStatus: ChipStatus | null
}) {
  if (!vm || !vm.available || vm.pocPrice === null) {
    // 不可用时展示结构化原因（来自 chipStatus），缺省才退回中性文案
    const fallbackText = chipStatus?.reasonText ?? '可选维度 · 暂无有效筹码峰'
    const stateLabel = chipStatus ? CHIP_STATE_LABEL[chipStatus.state] ?? null : null
    return (
      <div className={`${styles.dimCard} ${styles.dimChip} ${styles.optional}`}>
        <div className={styles.dimHeader}>
          <span className={styles.dimName}>筹码共识</span>
          {stateLabel && <span className={styles.dimBadge}>{stateLabel}</span>}
        </div>
        <div className={styles.dimEmpty}>{fallbackText}</div>
        {/* [QM-63] chip 溯源与新鲜度：有值才展示，缺失不编造 */}
        {chipStatus && <ChipProvenance chipStatus={chipStatus} />}
        {/* [CHANGE-20260730-010] M15_BARS_INSUFFICIENT 时展示诊断字段 */}
        {chipStatus?.reasonCode === 'M15_BARS_INSUFFICIENT'
          && chipStatus.actualBars !== null
          && chipStatus.actualBars !== undefined && (
          <div className={styles.metricRow}>
            <span className={styles.metric}>
              实际 <b>{chipStatus.actualBars}</b> 根
            </span>
            {chipStatus.requiredBars !== null
              && chipStatus.requiredBars !== undefined && (
              <span className={styles.metric}>
                最低 <b>{chipStatus.requiredBars}</b> 根
              </span>
            )}
            {chipStatus.fullQualityBars !== null
              && chipStatus.fullQualityBars !== undefined && (
              <span className={styles.metric}>
                完整门槛 <b>{chipStatus.fullQualityBars}</b> 根
              </span>
            )}
          </div>
        )}
      </div>
    )
  }
  // POC 位置轨道：±10% 范围映射，clamp 到 0~100%
  const distPct = vm.distancePct
  const markerPct = distPct !== null
    ? `${Math.min(100, Math.max(0, 50 + (distPct / 10) * 50))}%`
    : '50%'
  return (
    <div className={`${styles.dimCard} ${styles.dimChip} ${styles.optional}`}>
      <div className={styles.dimHeader}>
        <span className={styles.dimName}>筹码共识</span>
        {/* [QM-63] 有数据但非 ready（stale/partial）时仍须显示状态徽章 */}
        {chipStatus && CHIP_STATE_LABEL[chipStatus.state] && (
          <span className={styles.dimBadge} title={chipStatus.reasonText ?? undefined}>
            {CHIP_STATE_LABEL[chipStatus.state]}
          </span>
        )}
      </div>
      {/* POC 位置轨道 */}
      <div className={styles.pocTrack}>
        <span className={styles.pocLabel}>低位</span>
        <div className={styles.pocBar}>
          <div className={styles.pocCenter} title={`POC: ${vm.pocPrice}`} />
          <div className={styles.pocMarker} style={{ left: markerPct } as React.CSSProperties} title={`当前价: ${vm.lastClose ?? '-'}`} />
        </div>
        <span className={styles.pocLabel}>高位</span>
      </div>
      {/* 距离% + 峰数量 */}
      <div className={styles.metricRow}>
        {distPct !== null && (
          <span className={styles.metric}>距POC <b>{distPct.toFixed(2)}%</b></span>
        )}
        {vm.nPeakNodes !== null && (
          <span className={styles.metric}>峰数 <b>{vm.nPeakNodes}</b></span>
        )}
      </div>
      {/* [QM-63] chip 溯源与新鲜度 */}
      {chipStatus && <ChipProvenance chipStatus={chipStatus} />}
    </div>
  )
}

// ===== 主组件 =====

export function FirstPyramidPanel({ symbol, variant = 'detail', className }: FirstPyramidPanelProps) {
  const { data, isLoading, error, refetch } = useFirstPyramid(symbol)

  // [CHANGE-20260730-012] symbol 不一致不得无限显示加载中
  // 显示请求/响应标识错误 + 一次重试
  if (data && data.symbol !== symbol) {
    return (
      <div className={`${styles.error} ${className ?? ''}`}>
        <div className={styles.title}>第一金字塔</div>
        <div className={styles.status}>
          标识不匹配（请求={symbol}, 响应={data.symbol}）
        </div>
        <button type="button" onClick={() => refetch()} className={styles.retryBtn}>
          重试
        </button>
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
      <PyramidHeader tradeDate={vm.tradeDate} provenance={vm.provenance} />
      {/* compact 不渲染 statusText；detail 也不渲染聚合 statusText */}
      <StateRibbon vm={vm} />
      <VolumeWaterLevel vm={vm.volumeWaterLevel} />
      <div className={variant === 'compact' ? styles.dimensionsCompact : styles.dimensionsDetail}>
        <TrendVisualCard vm={vm.trend} variant={variant} />
        <StructureVisualCard vm={vm.structure} variant={variant} />
        <MomentumVisualCard vm={vm.momentum} variant={variant} />
        <ChipVisualCard vm={vm.chipConsensus} chipStatus={vm.chipStatus} />
      </div>
    </div>
  )
}
