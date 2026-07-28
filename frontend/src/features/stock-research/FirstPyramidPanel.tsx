// [Gate1] 第一金字塔统一快照面板 — 含统一量能水位条
// 调用 GET /api/v1/stocks/{symbol}/first-pyramid，展示趋势→结构→动量→筹码共识四维状态。
// - 固定维度顺序（ORDERED_DIMENSIONS），禁止页面动态组合
// - 前三维必选，chip_consensus 可选（无有效峰时为 null）
// - 状态文本由后端结构化结果生成，前端不重复算法判断
// - Gate1：共享 VolumeContext 水位条；事件量能徽标；趋势段均量比；动量缩量/放量转换
import { useFirstPyramid } from '@/hooks/useApi'
import type {
  DimensionResult,
  PyramidEvent,
  VolumeContextSchema,
} from '@/api/endpoints'

const DIMENSION_LABEL: Record<string, string> = {
  trend: '趋势',
  structure: '结构',
  momentum: '动量',
  chip_consensus: '筹码共识',
}

/** 格式化事件方向为中文 */
function formatDirection(dir: string | null): string {
  if (dir === 'up') return '上行'
  if (dir === 'down') return '下行'
  return '—'
}

/** 量能徽标颜色 */
function badgeClass(badge: string | null | undefined): string {
  if (badge === '放量') return 'fp-badge fp-badge-vol-up'
  if (badge === '缩量') return 'fp-badge fp-badge-vol-down'
  if (badge === '正常') return 'fp-badge fp-badge-vol-normal'
  return 'fp-badge fp-badge-vol-unknown'
}

/** 量能水位条 — 共享 20/200 日百分位与 z-score */
function VolumeWaterLevelBar({ vc }: { vc: VolumeContextSchema | null | undefined }) {
  if (!vc || !vc.readiness) {
    return (
      <div className="fp-volume-bar fp-volume-bar-na">
        <span className="fp-volume-label">量能水位</span>
        <span className="fp-volume-na">数据不足</span>
      </div>
    )
  }

  const pct20 = vc.volumePercentile20 ?? 0
  const pct200 = vc.volumePercentile200 ?? 0
  const z20 = vc.volumeZscore20
  const z200 = vc.volumeZscore200

  return (
    <div className="fp-volume-bar">
      <span className="fp-volume-label">量能水位</span>
      <div className="fp-volume-track-group">
        <div className="fp-volume-track">
          <span className="fp-volume-track-label">20日</span>
          <div className="fp-volume-scale">
            <div
              className="fp-volume-fill"
              style={{ width: `${Math.min(100, Math.max(0, pct20))}%` }}
            />
            <span className="fp-volume-pct">{pct20.toFixed(0)}</span>
          </div>
          {z20 !== null && (
            <span className="fp-volume-zscore">z={z20.toFixed(2)}</span>
          )}
        </div>
        <div className="fp-volume-track">
          <span className="fp-volume-track-label">200日</span>
          <div className="fp-volume-scale">
            <div
              className="fp-volume-fill fp-volume-fill-long"
              style={{ width: `${Math.min(100, Math.max(0, pct200))}%` }}
            />
            <span className="fp-volume-pct">{pct200.toFixed(0)}</span>
          </div>
          {z200 !== null && (
            <span className="fp-volume-zscore">z={z200.toFixed(2)}</span>
          )}
        </div>
      </div>
      <span className={badgeClass(vc.badge)}>{vc.badge ?? '未知'}</span>
    </div>
  )
}

/** 渲染单个事件（含量能徽标） */
function EventItem({ event }: { event: PyramidEvent }) {
  const parts: string[] = [event.type]
  if (event.direction) parts.push(formatDirection(event.direction))
  if (event.occurredAt) parts.push(event.occurredAt)
  if (event.price !== null && event.price !== undefined) parts.push(`价 ${event.price}`)
  parts.push(`新鲜度 ${event.freshnessBars ?? 0} 根`)
  return (
    <div className="fp-event-item">
      <span className="fp-event-text">{parts.join(' · ')}</span>
      {event.volumeBadge && (
        <span className={badgeClass(event.volumeBadge)}>{event.volumeBadge}</span>
      )}
    </div>
  )
}

/** 单维度卡片 */
function DimensionCard({ dim, optional }: { dim: DimensionResult; optional?: boolean }) {
  const label = DIMENSION_LABEL[dim.name] ?? dim.name
  const latestEvents = Array.isArray(dim.events) ? dim.events.slice(-3).reverse() : []
  const cf = dim.continuousFactors

  // 趋势卡：段均量比
  const segVolMean = cf.current_segment_volume_mean as number | undefined
  const vsPrevVol = cf.current_vs_prev_volume_ratio as number | undefined

  // 动量卡：缩量/放量转换
  const volDivergence = cf.vol_divergence as string | undefined
  const squeezeVolMean = cf.squeeze_period_volume_mean as number | undefined
  const releaseVsSqueeze = cf.release_vs_squeeze_volume_ratio as number | undefined

  return (
    <div className={`fp-dim-card${optional ? ' optional' : ''}`}>
      <div className="fp-dim-header">
        <span className="fp-dim-name">{label}</span>
        <span className={`fp-dim-badge ${dim.available ? 'ok' : 'na'}`}>
          {dim.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className="fp-dim-status">{dim.statusText}</div>

      {/* 趋势卡：段均量比 */}
      {dim.name === 'trend' && segVolMean !== undefined && segVolMean !== null && (
        <div className="fp-dim-vol-detail">
          段均量 {segVolMean.toFixed(0)}
          {vsPrevVol !== undefined && vsPrevVol !== null && (
            <span className="fp-vol-ratio">（vs前段 {vsPrevVol.toFixed(2)}x）</span>
          )}
        </div>
      )}

      {/* 动量卡：挤压期量能 */}
      {dim.name === 'momentum' && squeezeVolMean !== undefined && squeezeVolMean !== null && (
        <div className="fp-dim-vol-detail">
          挤压均量 {squeezeVolMean.toFixed(0)}
          {releaseVsSqueeze !== undefined && releaseVsSqueeze !== null && (
            <span className="fp-vol-ratio">（释放 {releaseVsSqueeze.toFixed(2)}x）</span>
          )}
          {volDivergence && (
            <span className={badgeClass(volDivergence === '放量释放' ? '放量' : '缩量')}>
              {volDivergence}
            </span>
          )}
        </div>
      )}

      {latestEvents.length > 0 && (
        <div className="fp-dim-events">
          {latestEvents.map((e, i) => (
            <EventItem key={`${e.type}-${i}`} event={e} />
          ))}
        </div>
      )}
    </div>
  )
}

export function FirstPyramidPanel({ symbol }: { symbol: string }) {
  const { data, isLoading, error } = useFirstPyramid(symbol)

  if (isLoading) {
    return (
      <div className="fp-panel fp-loading">
        <div className="fp-title">第一金字塔</div>
        <div className="fp-status">加载中...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="fp-panel fp-error">
        <div className="fp-title">第一金字塔</div>
        <div className="fp-status">
          加载失败：{error instanceof Error ? error.message : '未知错误'}
        </div>
      </div>
    )
  }

  if (!data) return null

  return (
    <div className="fp-panel">
      <div className="fp-header">
        <span className="fp-title">第一金字塔</span>
        <span className="fp-trade-date">{data.tradeDate}</span>
        <span className="fp-algo-version">{data.algorithmVersion}</span>
      </div>

      {/* 顶部综合状态 */}
      <div className="fp-summary">{data.statusText}</div>

      {/* Gate1：共享量能水位条 */}
      <VolumeWaterLevelBar vc={data.volumeContext} />

      {/* 四维卡片 */}
      <div className="fp-dimensions">
        <DimensionCard dim={data.trend} />
        <DimensionCard dim={data.structure} />
        <DimensionCard dim={data.momentum} />
        {data.chipConsensus ? (
          <DimensionCard dim={data.chipConsensus} optional />
        ) : (
          <div className="fp-dim-card optional">
            <div className="fp-dim-header">
              <span className="fp-dim-name">筹码共识</span>
              <span className="fp-dim-badge na">无有效峰</span>
            </div>
            <div className="fp-dim-status">无有效筹码峰，该维度可选</div>
          </div>
        )}
      </div>

      <div className="fp-footer">
        <span className="fp-hash">input: {data.inputHash.slice(0, 16)}...</span>
        <span className="fp-hash">param: {data.parameterHash.slice(0, 16)}...</span>
      </div>
    </div>
  )
}
