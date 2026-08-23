// [ScopeTrajectoryView] - 描述: Scope Trajectory 散点视图（Slice D）
// 坐标契约：x = summary.position（固定 0–100），y = summary.velocity。
// 仅绘制 position != null 且 velocity != null 的点；缺失值不强制为 0。
// acceleration 只作为中性方向字形（正 ▲ / 负 ▼ / 零 ■）绘制在对应 SVG 节点旁，
// 不转译成 bullish/bearish 颜色（红涨绿跌的方向色仅用于 EW Return / Capital Tilt
// 等市场方向值，acceleration 必须用中性 muted 字形）；品牌色仅用于选中节点描边。
// 不添加机会区标签；散点仅用于交互。
import type { ReviewScopeListItem } from './types'
import styles from './review.module.scss'

const W = 760
const H = 420
const PAD_L = 48
const PAD_R = 24
const PAD_T = 20
const PAD_B = 40

/** acceleration → 中性字形：正 ▲ / 负 ▼ / 零 ■；null/NaN 不绘制。
 *  无阈值、无业务解释、无分数；只做符号化，不做方向性解读。 */
function accelGlyphFor(value: number | null | undefined): string | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  if (value > 0) return '▲'
  if (value < 0) return '▼'
  return '■'
}

/** 列表项 acceleration 中性字形（随列表行显示，非主要展示位） */
function AccelGlyph({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  if (value > 0) return <span title="正 Acceleration">▲</span>
  if (value < 0) return <span title="负 Acceleration">▼</span>
  return <span title="零 Acceleration">■</span>
}

export interface ScopeTrajectoryViewProps {
  rows: ReviewScopeListItem[]
  selectedScopeKey: string | null
  onSelectScope: (scopeKey: string) => void
}

export default function ScopeTrajectoryView({ rows, selectedScopeKey, onSelectScope }: ScopeTrajectoryViewProps) {
  type PlotRow = ReviewScopeListItem & { summary: NonNullable<ReviewScopeListItem['summary']> }
  const plottable = rows.filter(
    (r): r is PlotRow =>
      r.summary !== null &&
      r.summary !== undefined &&
      r.summary.position !== null &&
      r.summary.position !== undefined &&
      r.summary.velocity !== null &&
      r.summary.velocity !== undefined,
  )

  if (plottable.length === 0) {
    return (
      <div className={styles.trajectoryEmpty}>
        没有可绘制的点：需要同时具备 Position 与 Velocity 的 Scope（缺失值不强制为 0）
      </div>
    )
  }

  // x 固定 0–100
  const xScale = (pos: number) => PAD_L + (pos / 100) * (W - PAD_L - PAD_R)
  // 计算 y 范围（仅用于图表缩放，非业务计算）
  const velocities = plottable.flatMap((r) => {
    const v = r.summary.velocity
    return v === null || v === undefined ? [] : [v]
  })
  let yMin = Math.min(...velocities, 0)
  let yMax = Math.max(...velocities, 0)
  if (yMin === yMax) {
    yMin -= 1
    yMax += 1
  }
  const yPad = (yMax - yMin) * 0.1
  yMin -= yPad
  yMax += yPad
  const yScale = (v: number) => PAD_T + ((yMax - v) / (yMax - yMin)) * (H - PAD_T - PAD_B)
  // y=0 参考线位置
  const yZero = yScale(0)

  const yTicks = 4
  const tickValues = Array.from({ length: yTicks + 1 }, (_, i) => yMin + ((yMax - yMin) / yTicks) * i)

  return (
    <div className={styles.trajectory}>
      <svg viewBox={`0 0 ${W} ${H}`} className={styles.trajectorySvg} role="img" aria-label="Scope 轨迹散点图">
        {/* y=0 参考线 */}
        <line x1={PAD_L} y1={yZero} x2={W - PAD_R} y2={yZero} className={styles.trajZeroLine} />
        {/* y 刻度 */}
        {tickValues.map((v) => {
          const y = yScale(v)
          return (
            <g key={v}>
              <line x1={PAD_L} y1={y} x2={W - PAD_R} y2={y} className={styles.trajGridLine} />
              <text x={PAD_L - 6} y={y + 3} textAnchor="end" className={styles.trajTickText}>
                {v.toFixed(2)}
              </text>
            </g>
          )
        })}
        {/* x 轴 0–100 刻度 */}
        {[0, 25, 50, 75, 100].map((v) => (
          <text
            key={v}
            x={xScale(v)}
            y={H - PAD_B + 16}
            textAnchor="middle"
            className={styles.trajTickText}
          >
            {v}
          </text>
        ))}
        <text x={(PAD_L + W - PAD_R) / 2} y={H - 6} textAnchor="middle" className={styles.trajAxisLabel}>
          Position (0–100)
        </text>
        {/* 节点（含 acceleration 中性字形） */}
        {plottable.map((r) => {
          const pos = r.summary.position as number
          const vel = r.summary.velocity as number
          const cx = xScale(pos)
          const cy = yScale(vel)
          const selected = r.scopeKey === selectedScopeKey
          const rNode = selected ? 6 : 5
          const accel = accelGlyphFor(r.summary.acceleration)
          return (
            <g
              key={r.scopeKey}
              className={selected ? styles.trajNodeSelected : styles.trajNode}
              onClick={() => onSelectScope(r.scopeKey)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onSelectScope(r.scopeKey)
                }
              }}
              tabIndex={0}
              role="button"
              aria-label={`Scope ${r.scopeName ?? r.scopeKey}`}
            >
              <title>{`${r.scopeName ?? r.scopeKey} · velocity ${vel.toFixed(2)}`}</title>
              <circle cx={cx} cy={cy} r={rNode} />
              {accel !== null && (
                <text
                  x={cx + rNode + 4}
                  y={cy + 1}
                  className={styles.trajAccelText}
                  aria-hidden="true"
                >
                  {accel}
                </text>
              )}
            </g>
          )
        })}
        {/* y 轴标题 */}
        <text
          x={14}
          y={(PAD_T + H - PAD_B) / 2}
          textAnchor="middle"
          transform={`rotate(-90 14 ${(PAD_T + H - PAD_B) / 2})`}
          className={styles.trajAxisLabel}
        >
          Velocity
        </text>
      </svg>
      <div className={styles.trajLegend}>
        <span className={styles.trajLegendItem}>
          <span className={styles.accelGlyph}>▲</span> 正 Acceleration
        </span>
        <span className={styles.trajLegendItem}>
          <span className={styles.accelGlyph}>▼</span> 负 Acceleration
        </span>
        <span className={styles.trajLegendItem}>
          <span className={styles.accelGlyph}>■</span> 零 Acceleration
        </span>
        <span className={styles.trajLegendItem}>● 节点中性色（选中为品牌描边）</span>
      </div>
      <div className={styles.trajList}>
        {plottable.map((r) => (
          <button
            key={r.scopeKey}
            type="button"
            className={r.scopeKey === selectedScopeKey ? `${styles.trajListItem} ${styles.trajListItemSelected}` : styles.trajListItem}
            onClick={() => onSelectScope(r.scopeKey)}
          >
            {r.scopeName ?? r.scopeKey}
            <span className={styles.trajItemAccel}>
              <AccelGlyph value={r.summary.acceleration} />
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}