// [Phase 5B-2] 第一金字塔统一快照面板
// 调用 GET /api/v1/stocks/{symbol}/first-pyramid，展示趋势→结构→动量→筹码共识四维状态。
// - 固定维度顺序（ORDERED_DIMENSIONS），禁止页面动态组合
// - 前三维必选，chip_consensus 可选（无有效峰时为 null）
// - 状态文本由后端结构化结果生成，前端不重复算法判断
// - 事件按时间升序展示最新 3 条
import { useFirstPyramid } from '@/hooks/useApi'
import type { DimensionResult, PyramidEvent } from '@/api/endpoints'

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

/** 渲染单个事件（紧凑展示） */
function EventItem({ event }: { event: PyramidEvent }) {
  const parts: string[] = [event.type]
  if (event.direction) parts.push(formatDirection(event.direction))
  if (event.occurredAt) parts.push(event.occurredAt)
  if (event.price !== null) parts.push(`价 ${event.price}`)
  parts.push(`新鲜度 ${event.freshnessBars} 根`)
  return <div className="fp-event-item">{parts.join(' · ')}</div>
}

/** 单维度卡片 */
function DimensionCard({ dim, optional }: { dim: DimensionResult; optional?: boolean }) {
  const label = DIMENSION_LABEL[dim.name] ?? dim.name
  const latestEvents = dim.events.slice(-3).reverse()
  return (
    <div className={`fp-dim-card${optional ? ' optional' : ''}`}>
      <div className="fp-dim-header">
        <span className="fp-dim-name">{label}</span>
        <span className={`fp-dim-badge ${dim.available ? 'ok' : 'na'}`}>
          {dim.available ? '可用' : '无数据'}
        </span>
      </div>
      <div className="fp-dim-status">{dim.statusText}</div>
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
      <div className="fp-summary">{data.statusText}</div>
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
