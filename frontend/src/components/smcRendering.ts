// [SmcRendering] - 描述: SMC 渲染纯函数与类型（无 React / Canvas 依赖，可被 node --test 直接运行）
// 抽离自 StrategyChart.tsx，确保 SMC 渲染逻辑可独立测试（PROMPT.md §四.4）
//
// 设计原则：
//   - view adapter 已将所有 SMC 索引重基准到展示窗口坐标系
//     rebased = original_full_history_index - offset
//     offset = max(0, total_bars - display_bars)
//   - 本模块的纯函数只处理重基准后的索引，不依赖时间匹配（adapter 保证 1:1 对齐）
//   - 完全排除 FVG：类型与函数均不涉及 Fair Value Gap

// ===== SMC 类型定义（与后端 DTO 一致）=====

export interface SmcEvent {
  type: 'BOS' | 'CHoCH'
  bias: number  // 1=bullish, -1=bearish
  internal?: boolean  // true=internal structure, false/undefined=swing
  anchor_index: number
  anchor_time: string | null
  confirmed_index: number
  confirmed_time: string | null
  level?: number | null
}

export interface SmcOrderBlock {
  // CHANGE-20260715-007: 使用 anchor_index/anchor_time（旧 bar_index/bar_time 已修正）
  anchor_index: number
  anchor_time: string
  bar_high: number
  bar_low: number
  bias: number  // 1=bullish, -1=bearish
  internal?: boolean  // true=internal OB, false/undefined=swing OB
  confirmed_index: number
  confirmed_time: string
  mitigated: boolean
  mitigated_index?: number | null
  mitigated_time?: string | null
  // CHANGE-20260715-007: view adapter 标记 anchor 在窗口左侧（需 clamp 到 plotLeft）
  clipped_left?: boolean
}

export interface SmcEqualHighLow {
  type: 'EQH' | 'EQL'
  anchor_index: number
  anchor_time: string | null
  // CHANGE-20260715-007: 新 pivot 所在 bar（= ref_i = i-size），线段终点
  second_pivot_index: number
  second_pivot_time: string | null
  // CHANGE-20260715-007: leg change 检测 bar（= i），因果确认点（用于回放测试，非线段终点）
  confirmed_index: number
  confirmed_time: string | null
  level: number
  prev_level: number
}

export interface SmcTrailing {
  top: number | null
  bottom: number | null
  bar_time: string | null
  bar_index: number | null
  last_top_time: string | null
  last_bottom_time: string | null
}

// CHANGE-20260715-007: view adapter 元信息
export interface SmcView {
  total_bars: number
  display_bars: number
  offset: number
  window_start: number
  window_end: number
}

// CHANGE-20260715-007: swing_bias 为数值（1=bullish, -1=bearish, 0=未形成趋势）
// 前端规则：bias===-1 → Strong High（否则 Weak High）；bias===1 → Strong Low（否则 Weak Low）
export type SmcSwingBias = 1 | -1 | 0

// SMC 配色（盘迹 V1：A 股上涨结构红，下跌结构绿）
export const SMC_BULL_COLOR = '#FF4D4F'   // 上涨结构（bias=1）
export const SMC_BEAR_COLOR = '#22C55E'   // 下跌结构（bias=-1）

/** 可见窗口上下文（用于索引范围判断） */
export interface SmcVisibleContext {
  /** Number of bars in the visible display window (= displayTimes.length). */
  displayCount: number
}

/**
 * Map a (rebased) SMC index to a K-line display index.
 *
 * view adapter 已将所有 SMC 索引重基准到展示窗口坐标系，因此本函数只需处理：
 *   - null/undefined → undefined（不可见）
 *   - 负索引 → 0（OB clipped_left 时 anchor 在窗口左侧，clamp 到窗口左端）
 *   - 索引 >= displayCount → undefined（在窗口右侧，不可见）
 *   - 其他 → 直接返回（已在展示坐标系）
 *
 * 注：StrategyChart 的 smcToDisplay 在此基础上再做时间匹配作为防御性回退，
 *     但在 adapter 正常工作时直接使用本函数即可。
 */
export function mapSmcIndexToDisplay(
  smcIdx: number | null | undefined,
  ctx: SmcVisibleContext,
): number | undefined {
  if (smcIdx == null) return undefined
  if (smcIdx < 0) return 0  // clipped_left: clamp to display left
  if (smcIdx >= ctx.displayCount) return undefined
  return smcIdx
}

/**
 * Select visible Order Blocks for rendering.
 *
 * 规则（PROMPT.md §四.2）：
 *   - 只画 internal===true && mitigated===false
 *   - 后端最新 OB 在数组头部 → slice(0, 5)
 *   - 与 viewport 无交集时跳过（anchor 在窗口右侧 → 跳过）
 *   - clipped_left 时 anchor 在窗口左侧 → 保留（不跳过），前端 clamp x1 到 plotLeft
 *
 * 返回顺序保持后端原始顺序（最新 OB 在数组头部 → 渲染时最新 OB 先画，
 * 与 PROMPT.md "后端最新OB在数组头部，取slice(0,5)" 一致）。
 */
export function selectVisibleSmcOrderBlocks(
  orderBlocks: SmcOrderBlock[],
  ctx: SmcVisibleContext,
): SmcOrderBlock[] {
  // 只选 internal && !mitigated
  const candidates = orderBlocks.filter(ob => ob.internal === true && !ob.mitigated)
  // 后端最新 OB 在数组头部 → 取前 5 个
  const top5 = candidates.slice(0, 5)
  // 与 viewport 无交集时跳过：
  //   - anchor 重基准后 < displayCount（在窗口左侧或窗口内）→ 可见
  //   - anchor >= displayCount（在窗口右侧）→ 不可见
  //   - clipped_left（anchor 为负）→ 仍可见（mapSmcIndexToDisplay clamp 到 0）
  return top5.filter(ob => {
    const anchorIdx = mapSmcIndexToDisplay(ob.anchor_index, ctx)
    return anchorIdx != null
  })
}

/**
 * Collect price candidates from visible SMC elements for y-axis range.
 *
 * PROMPT.md §四.3：主图纵轴候选加入当前可见的：
 *   - event.level（anchor 或 confirmed 在窗口内）
 *   - OB bar_high/bar_low（仅选中的 5 个 internal+unmitigated OB）
 *   - EQH/EQL level（anchor 或 second_pivot 在窗口内）
 *   - trailing top/bottom
 *
 * 目的：避免 SMC 元素被画出 Canvas（纵轴范围必须包含所有可见 SMC 价格）。
 */
export function collectVisibleSmcPriceCandidates(
  smcData: {
    events?: SmcEvent[]
    order_blocks?: SmcOrderBlock[]
    equal_highs_lows?: SmcEqualHighLow[]
    trailing?: SmcTrailing | null
    swing_bias?: number
  },
  ctx: SmcVisibleContext,
): number[] {
  const candidates: number[] = []

  // event.level（anchor 或 confirmed 在窗口内）
  for (const ev of smcData.events ?? []) {
    if (ev.level == null) continue
    const aIdx = mapSmcIndexToDisplay(ev.anchor_index, ctx)
    const cIdx = mapSmcIndexToDisplay(ev.confirmed_index, ctx)
    if (aIdx != null || cIdx != null) {
      candidates.push(ev.level)
    }
  }

  // OB bar_high/bar_low: 仅选中的 5 个 internal+unmitigated OB
  const visibleObs = selectVisibleSmcOrderBlocks(smcData.order_blocks ?? [], ctx)
  for (const ob of visibleObs) {
    candidates.push(ob.bar_high, ob.bar_low)
  }

  // EQH/EQL level（anchor 或 second_pivot 在窗口内）
  for (const eq of smcData.equal_highs_lows ?? []) {
    const aIdx = mapSmcIndexToDisplay(eq.anchor_index, ctx)
    const spIdx = mapSmcIndexToDisplay(eq.second_pivot_index, ctx)
    if (aIdx != null || spIdx != null) {
      candidates.push(eq.level)
    }
  }

  // trailing top/bottom（始终视为可见，因为 trailing 表示当前最新结构极值）
  if (smcData.trailing) {
    if (smcData.trailing.top != null) candidates.push(smcData.trailing.top)
    if (smcData.trailing.bottom != null) candidates.push(smcData.trailing.bottom)
  }

  return candidates
}

/**
 * Viewport intersection result for a SMC range [anchorIdx, confirmedIdx].
 *
 * PROMPT.md §三.2：只要区间与viewport相交就绘制：
 *   - anchor在左侧时 startIdx=0（plotLeft）
 *   - confirmed在右侧时 endIdx=displayCount-1（plotRight）
 *   - 仅完全不相交时返回 null（跳过）
 */
export interface SmcViewportRange {
  /** Start display index (clamped to 0 if anchor is before viewport). */
  startIdx: number
  /** End display index (clamped to displayCount-1 if confirmed is after viewport). */
  endIdx: number
  /** True if anchor was clamped to viewport left (anchor before viewport). */
  clippedLeft: boolean
  /** True if confirmed was clamped to viewport right (confirmed after viewport). */
  clippedRight: boolean
}

/**
 * Compute the viewport intersection of a SMC range [anchorIdx, confirmedIdx].
 *
 * view adapter 已将索引重基准到展示坐标系：
 *   - 负值 = anchor 在窗口左侧（clipped_left）
 *   - >= displayCount = 在窗口右侧
 *
 * 返回 null 表示区间与 viewport 完全不相交（都在左侧或都在右侧），应跳过。
 * 否则返回 clamp 后的 [startIdx, endIdx] 及 clipped 标记。
 *
 * 仅要求 anchor <= confirmed（因果方向），不要求两者都在窗口内。
 */
export function intersectSmcRangeWithViewport(
  anchorIdx: number | null | undefined,
  confirmedIdx: number | null | undefined,
  ctx: SmcVisibleContext,
): SmcViewportRange | null {
  if (anchorIdx == null || confirmedIdx == null) return null
  if (confirmedIdx < anchorIdx) return null  // 因果方向错误
  // 都在窗口左侧 → 完全不相交
  if (confirmedIdx < 0) return null
  // 都在窗口右侧 → 完全不相交
  if (anchorIdx >= ctx.displayCount) return null
  // 区间与 viewport 相交 → clamp
  const startIdx = Math.max(0, anchorIdx)
  const endIdx = Math.min(ctx.displayCount - 1, confirmedIdx)
  return {
    startIdx,
    endIdx,
    clippedLeft: anchorIdx < 0,
    clippedRight: confirmedIdx >= ctx.displayCount,
  }
}

/** hex 颜色转 rgba（用于 OB 区域透明度控制） */
export function hexToRgba(hex: string, alpha: number): string {
  // 支持 #RRGGBB 和 #RGB 格式
  const cleaned = hex.replace('#', '')
  let r: number, g: number, b: number
  if (cleaned.length === 3) {
    r = parseInt(cleaned[0] + cleaned[0], 16)
    g = parseInt(cleaned[1] + cleaned[1], 16)
    b = parseInt(cleaned[2] + cleaned[2], 16)
  } else if (cleaned.length === 6) {
    r = parseInt(cleaned.substring(0, 2), 16)
    g = parseInt(cleaned.substring(2, 4), 16)
    b = parseInt(cleaned.substring(4, 6), 16)
  } else {
    // 无法解析，返回原始 hex
    return hex
  }
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}
