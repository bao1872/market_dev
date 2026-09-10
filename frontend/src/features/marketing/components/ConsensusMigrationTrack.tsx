// 共识迁移轨迹（V1）— 真实 POC 的离散 step track（M19 视觉高潮）。
// 只显示 production frame.summary.primaryConsensusPrice（真实 POC snapshot），
// 不插值、不做 linear spline、不制造不存在的中间共识价。
// 用 step-after（水平→垂直→水平）而非 tween：POC 是离散 snapshot 状态。
// 复用的区域关系：只有真实迁移（如 35.x → 45.x）才产生明确台阶跳升。
import { useMemo } from 'react'

export interface ConsensusPoint {
  endIndex: number
  endTime: string
  price: number | null
}

export interface ConsensusMigrationTrackProps {
  /** 全部 canonical 帧的 POC 采样。 */
  points: ConsensusPoint[]
  /** 可见窗口起点 endIndex（回放从第 1 帧开始前，may have 30）。 */
  startEndIndex: number
  /** 可见窗口终点 endIndex（＝ bars.length）。 */
  endEndIndex: number
  /** 当前可见位置（用于标出「当前共识价」游标，无未来）。 */
  visibleEndIndex: number
  width?: number
  height?: number
  /** 视觉 token（由父级从 languages/token 取值传入，本组件不硬编码颜色）。 */
  colors: {
    track: string
    cursor: string
    bg: string
    panel: string
    border: string
    text: string
    textDim: string
  }
}

const WIDTH = 1120
const HEIGHT = 120
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 10
const PAD_BOTTOM = 22

export default function ConsensusMigrationTrack({
  points,
  startEndIndex,
  endEndIndex,
  visibleEndIndex,
  width = WIDTH,
  height = HEIGHT,
  colors,
}: ConsensusMigrationTrackProps) {
  const chart = useMemo(() => {
    const prices = points
      .map((p) => p.price)
      .filter((p): p is number => p != null)
    if (!prices.length) return null

    let min = Math.min(...prices)
    let max = Math.max(...prices)
    // 保证至少一条可读范围；上下留 4% 余量。
    const span = Math.max(max - min, 1e-6)
    min -= span * 0.08
    max += span * 0.08

    const innerW = width - PAD_LEFT - PAD_RIGHT
    const innerH = height - PAD_TOP - PAD_BOTTOM
    const x = (endIndex: number) =>
      PAD_LEFT + ((endIndex - startEndIndex) / Math.max(1, endEndIndex - startEndIndex)) * innerW
    const y = (price: number) => PAD_TOP + (1 - (price - min) / (max - min)) * innerH

    // step track 顶点：每个点 → 水平段到 x，再垂直段到 y。
    const visible = points.filter((p) => p.endIndex <= visibleEndIndex)
    const path =
      visible.length > 0
        ? visible
            .map((p, i) => {
              if (p.price == null) return ''
              const px = x(p.endIndex)
              const py = y(p.price)
              if (i === 0) return `M ${px} ${py}`
              return `H ${px} L ${px} ${py}`
            })
            .join(' ')
        : ''

    return { x, y, path, min, max }
  }, [points, startEndIndex, endEndIndex, visibleEndIndex, width, height])

  if (!chart) {
    return null
  }

  const labelMin = chart.min.toFixed(2)
  const labelMax = chart.max.toFixed(2)

  return (
    <div className="consensus-track" data-testid="marketing-consensus-track">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label="主要成交密集价随时间的迁移轨迹"
        preserveAspectRatio="none"
      >
        {/* 纵向范围标签 */}
        <text x={PAD_LEFT} y={PAD_TOP + 2} fontSize="9" fill={colors.textDim}>
          {labelMax}
        </text>
        <text x={PAD_LEFT} y={height - PAD_BOTTOM} fontSize="9" fill={colors.textDim}>
          {labelMin}
        </text>

        {/* step track：水平→垂直→水平，离散真实 POC */}
        <path
          d={chart.path}
          fill="none"
          stroke={colors.track}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* 当前共识价游标（最近 1 个不超前帧的 POC） */}
        {(() => {
          const cur = [...points]
            .reverse()
            .find((p) => p.price != null && p.endIndex <= visibleEndIndex)
          if (!cur || cur.price == null) return null
          return (
            <g>
              <circle
                cx={chart.x(cur.endIndex)}
                cy={chart.y(cur.price)}
                r={3.5}
                fill={colors.cursor}
                stroke={colors.bg}
                strokeWidth={1.5}
              />
              <rect
                x={Math.min(chart.x(cur.endIndex) + 6, width - 86)}
                y={Math.max(chart.y(cur.price) - 26, 0)}
                width={80}
                height={14}
                rx={3}
                fill={colors.panel}
                stroke={colors.border}
              />
              <text
                x={Math.min(chart.x(cur.endIndex) + 11, width - 81)}
                y={Math.max(chart.y(cur.price) - 16, 2)}
                fontSize="9"
                fill={colors.text}
              >
                {cur.price.toFixed(2)}
              </text>
            </g>
          )
        })()}
      </svg>
    </div>
  )
}