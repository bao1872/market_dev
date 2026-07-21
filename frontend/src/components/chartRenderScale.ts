// [PROMPT.md §5.3.4 V2] 集中管理 Canvas 字体 / 线宽 / 几何尺寸缩放。
//
// 背景：1440×2560 移动舞台下，原来 desktop 模式的 8-11px Canvas 字体在截图中几乎不可读。
//       PROMPT.md §5.3.4 要求为 StrategyChart 增加 renderDensity: 'desktop' | 'mobile_capture'，
//       集中维护 ChartTypography / ChartStrokeScale / ChartGeometryScale，
//       禁止 renderer 零散乘倍数（避免某些标签遗漏放大、某些线宽被双重放大）。
//
// 设计：
//   - desktop 保持当前所有数值不变（向后兼容，PC 端浏览体验不受影响）
//   - mobile_capture 按 §5.3.4 规范表设置：
//       · Canvas 价格轴 ≥32px、时间轴 ≥30px
//       · POC/Node/BB 标签 34-40px
//       · SMC internal 标签 ≥28px、SMC swing 标签 ≥34px
//       · 图例 30-34px
//       · 网格线宽 1.5-2px、BB/结构线 2.5-3.5px、POC 3-4px、K线最小实体宽 ≥4px
//
// 使用：
//   const scale = getRenderScale(renderDensity)
//   drawText(ctx, '...', x, y, color, scale.fonts.axisLabel)
//   drawLine(ctx, x1, y1, x2, y2, color, scale.strokes.grid)
//
// 修改原则：renderer 不得直接写 '8px monospace' / lineWidth = 1.5 等魔法数字，
//           必须从 scale.fonts.* / scale.strokes.* / scale.geometry.* 读取。

export type RenderDensity = 'desktop' | 'mobile_capture'

/**
 * Canvas 字体规格（PROMPT.md §5.3.4 字号表）。
 *
 * 字段命名按用途分组，renderer 按用途选择，不按字号选择：
 *   - axisLabel: 价格轴 / 时间轴刻度
 *   - paneLabel: 副图标题（如 MACD / SQZMOM）
 *   - paneTick: 副图刻度数字
 *   - paneCurrent: 副图当前值标签
 *   - vaLabel: VAH / VAL / Value Area 标注
 *   - profileLabel: 成交量分布 买卖量 头部标签
 *   - structureLabel: 结构压力位等结构标注
 *   - nodeLabel: Node 区间主标签（含 POC）
 *   - nodeVolLabel: Node 成交量副标签
 *   - pocLabel: 核心共识价行
 *   - smcInternalLabel: SMC internal 结构标签
 *   - smcSwingLabel: SMC swing 结构标签
 *   - eqLabel: EQH/EQL 标签
 *   - smcBoundLabel: SMC BOS/CHoCH 端点标签
 *   - legend: 十字线联动 OHLC 图例
 *   - emptyHint: "暂不可用"等空态提示
 */
export interface ChartTypography {
  axisLabel: string
  paneLabel: string
  paneTick: string
  paneCurrent: string
  vaLabel: string
  profileLabel: string
  structureLabel: string
  nodeLabel: string
  nodeVolLabel: string
  pocLabel: string
  smcInternalLabel: string
  smcSwingLabel: string
  eqLabel: string
  smcBoundLabel: string
  legend: string
  legendBold: string
  emptyHint: string
}

/**
 * Canvas 线宽规格（PROMPT.md §5.3.4 线宽要求）。
 *
 *   - grid: 主网格（横线）1.5-2px
 *   - grid2: 次网格（垂直虚线）1-1.5px
 *   - paneSep: 副图分隔线 1-1.5px
 *   - candleWick: K 线影线 1-2px
 *   - candleBodyMin: K 线最小实体宽 ≥4px（mobile_capture 必须）
 *   - bbLine: BB 三轨线 2.5-3.5px
 *   - dsaVwap: DSA VWAP 主线 2-3px
 *   - dsaPolyline: DSA 多段线 1.5-2.5px
 *   - macdDif: MACD DIF 线 1.5-2px
 *   - macdDea: MACD DEA 线 1.5-2px
 *   - sqzMomLine: SQZMOM 线 1.5-2px
 *   - nodeLine: Node 区间边线 1.5-2.5px
 *   - profileBarBorder: 成交量分布条边线 1-2px
 *   - vaLine: VAH/VAL 水平虚线 1.5-2px
 *   - pocLine: POC 水平粗线 3-4px
 *   - smcInternal: SMC internal 结构线 1.5-2px（虚线）
 *   - smcSwing: SMC swing 结构线 2.5-3.5px（实线）
 *   - obBorder: OB 区块边线 1.5-2px
 *   - eqLine: EQH/EQL 水平线 1.5-2px
 *   - eventMarker: 事件标记竖线 1-2px
 */
export interface ChartStrokeScale {
  grid: number
  grid2: number
  paneSep: number
  candleWick: number
  candleBodyMin: number
  bbLine: number
  dsaVwap: number
  dsaPolyline: number
  macdDif: number
  macdDea: number
  sqzMomLine: number
  nodeLine: number
  profileBarBorder: number
  vaLine: number
  pocLine: number
  smcInternal: number
  smcSwing: number
  obBorder: number
  eqLine: number
  eventMarker: number
}

/**
 * Canvas 几何尺寸规格（轴宽 / Profile 宽 / 间距 / 节点半径）。
 *
 * mobile_capture 下需放大以容纳更大字号与可读节点标记。
 */
export interface ChartGeometryScale {
  /** 右侧价格轴宽度（容纳 axisLabel 文本） */
  axisWidth: number
  /** 左侧 Volume Profile 宽度 */
  profileWidth: number
  /** Profile 与绘图区之间的间距 */
  profileGap: number
  /** Node 标记圆点半径 */
  nodeMarkerRadius: number
  /** 副图当前值标签背景宽度 */
  paneCurrentBoxWidth: number
  /** 副图当前值标签背景高度 */
  paneCurrentBoxHeight: number
  /** 图例行间距 */
  legendLineHeight: number
  /** 事件标记竖线延伸到价格区的占比（0-1） */
  eventMarkerExtent: number
}

export interface ChartRenderScale {
  density: RenderDensity
  fonts: ChartTypography
  strokes: ChartStrokeScale
  geometry: ChartGeometryScale
}

// ===== Desktop 模式（保持现有数值，向后兼容）=====
// 字体沿用 StrategyChart V1.6.4 既有魔法数字（8-11px）
const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'
const SANS = 'sans-serif'

export const DESKTOP_SCALE: ChartRenderScale = {
  density: 'desktop',
  fonts: {
    axisLabel: `10px ${MONO}`,
    paneLabel: `9px ${SANS}`,
    paneTick: `8px ${MONO}`,
    paneCurrent: `8px ${MONO}`,
    vaLabel: `8px ${SANS}`,
    profileLabel: `8px ${SANS}`,
    structureLabel: `8px ${SANS}`,
    nodeLabel: `11px ${SANS}`,
    nodeVolLabel: `9px ${SANS}`,
    pocLabel: `9px ${SANS}`,
    smcInternalLabel: `8px ${SANS}`,
    smcSwingLabel: `11px ${SANS}`,
    eqLabel: `8px ${SANS}`,
    smcBoundLabel: `8px ${SANS}`,
    legend: `9px ${MONO}`,
    legendBold: `bold 9px ${MONO}`,
    emptyHint: `11px ${SANS}`,
  },
  strokes: {
    grid: 1,
    grid2: 1,
    paneSep: 1,
    candleWick: 1,
    candleBodyMin: 2,
    bbLine: 1.5,
    dsaVwap: 1.5,
    dsaPolyline: 1.2,
    macdDif: 1.2,
    macdDea: 1.2,
    sqzMomLine: 1.5,
    nodeLine: 1.2,
    profileBarBorder: 1,
    vaLine: 1,
    pocLine: 1.4,
    smcInternal: 1,
    smcSwing: 1.5,
    obBorder: 1.5,
    eqLine: 1.5,
    eventMarker: 1,
  },
  geometry: {
    axisWidth: 60,
    profileWidth: 180,
    profileGap: 12,
    nodeMarkerRadius: 3,
    paneCurrentBoxWidth: 54,
    paneCurrentBoxHeight: 14,
    legendLineHeight: 14,
    eventMarkerExtent: 1,
  },
}

// ===== mobile_capture 模式（PROMPT.md §5.3.4 规范表）=====
// 字号：价格轴 32px / 时间轴 30px / POC/Node/BB 36px / SMC internal 30px / SMC swing 36px / 图例 30px
// 线宽：grid 1.8px / BB 3px / POC 3.5px / K线最小实体 4px / SMC swing 3px
// 几何：axis 130px / profile 360px / gap 24px（容纳大字号）
export const MOBILE_CAPTURE_SCALE: ChartRenderScale = {
  density: 'mobile_capture',
  fonts: {
    // 价格轴 / 时间轴（≥32px / ≥30px）
    axisLabel: `32px ${MONO}`,
    paneLabel: `30px ${SANS}`,
    paneTick: `30px ${MONO}`,
    paneCurrent: `30px ${MONO}`,
    // VAH / VAL / Value Area 标注（34-40px）
    vaLabel: `34px ${SANS}`,
    // Profile 买卖量头部标签（34-40px）
    profileLabel: `34px ${SANS}`,
    // 结构压力位标注（34-40px）
    structureLabel: `34px ${SANS}`,
    // Node 区间主标签（34-40px）
    nodeLabel: `36px ${SANS}`,
    nodeVolLabel: `30px ${SANS}`,
    pocLabel: `32px ${SANS}`,
    // SMC internal 标签（≥28px）
    smcInternalLabel: `30px ${SANS}`,
    // SMC swing 标签（≥34px）
    smcSwingLabel: `36px ${SANS}`,
    // EQH/EQL 标签（34-40px）
    eqLabel: `34px ${SANS}`,
    // SMC BOS/CHoCH 端点标签
    smcBoundLabel: `30px ${SANS}`,
    // 图例（30-34px）
    legend: `30px ${MONO}`,
    legendBold: `bold 30px ${MONO}`,
    emptyHint: `32px ${SANS}`,
  },
  strokes: {
    // 网格 1.5-2px
    grid: 1.8,
    grid2: 1.5,
    paneSep: 1.5,
    // K 线影线
    candleWick: 2.5,
    // K 线最小实体宽 ≥4px
    candleBodyMin: 4,
    // BB / 结构线 2.5-3.5px
    bbLine: 3,
    dsaVwap: 3,
    dsaPolyline: 2.5,
    macdDif: 2,
    macdDea: 2,
    sqzMomLine: 2.5,
    nodeLine: 2.5,
    profileBarBorder: 2,
    vaLine: 2,
    // POC 水平粗线 3-4px
    pocLine: 3.5,
    // SMC internal 1.5-2px（虚线）
    smcInternal: 2,
    // SMC swing 2.5-3.5px（实线）
    smcSwing: 3,
    obBorder: 2,
    eqLine: 2,
    eventMarker: 2,
  },
  geometry: {
    // 容纳 32px 价格轴文本
    axisWidth: 130,
    // 容纳大字号 Profile 标签
    profileWidth: 360,
    profileGap: 24,
    nodeMarkerRadius: 8,
    // 容纳 30px 副图当前值文本
    paneCurrentBoxWidth: 180,
    paneCurrentBoxHeight: 44,
    legendLineHeight: 44,
    eventMarkerExtent: 1,
  },
}

export function getRenderScale(density: RenderDensity = 'desktop'): ChartRenderScale {
  return density === 'mobile_capture' ? MOBILE_CAPTURE_SCALE : DESKTOP_SCALE
}
