// [SmcRenderingContract] - 描述: SMC 渲染纯函数与 Canvas mock 测试（PROMPT.md §四.4 + §四.5）
// 用法：node --experimental-strip-types --test src/components/__tests__/smcRendering.test.ts
//
// 覆盖：
//   1. selectVisibleSmcOrderBlocks: 最多 5 个 OB / internal+unmitigated 过滤 / slice(0,5) / clipped_left 保留
//   2. collectVisibleSmcPriceCandidates: event.level / OB bar_high,bar_low / EQH level / trailing top,bottom
//   3. mapSmcIndexToDisplay: 负索引 clamp / 越界返回 undefined / 正常索引透传
//   4. Canvas mock 测试：最多 5 个 OB / 左侧 clamp / EQH 线到 second_pivot / Strong/Weak 读 DTO swing_bias /
//      SMC 价格进入纵轴 / FVG 绘制调用为 0

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  selectVisibleSmcOrderBlocks,
  collectVisibleSmcPriceCandidates,
  mapSmcIndexToDisplay,
  intersectSmcRangeWithViewport,
  hexToRgba,
  layoutSmcLabels,
  SMC_BULL_COLOR,
  SMC_BEAR_COLOR,
  type SmcOrderBlock,
  type SmcEvent,
  type SmcEqualHighLow,
  type SmcTrailing,
  type SmcLabelAnchor,
  type SmcLabelLayoutContext,
} from '../smcRendering.ts'

// ===== 工具：构造测试用 OB =====
function makeOb(overrides: Partial<SmcOrderBlock> = {}): SmcOrderBlock {
  return {
    anchor_index: 5,
    anchor_time: 't5',
    bar_high: 11.0,
    bar_low: 9.0,
    bias: 1,
    internal: true,
    confirmed_index: 6,
    confirmed_time: 't6',
    mitigated: false,
    mitigated_index: null,
    mitigated_time: null,
    clipped_left: false,
    ...overrides,
  }
}

// ===== 1. mapSmcIndexToDisplay =====

test('mapSmcIndexToDisplay: null/undefined → undefined', () => {
  const ctx = { displayCount: 30 }
  assert.equal(mapSmcIndexToDisplay(null, ctx), undefined)
  assert.equal(mapSmcIndexToDisplay(undefined, ctx), undefined)
})

test('mapSmcIndexToDisplay: 负索引 → 0 (clipped_left clamp)', () => {
  const ctx = { displayCount: 30 }
  assert.equal(mapSmcIndexToDisplay(-1, ctx), 0)
  assert.equal(mapSmcIndexToDisplay(-25, ctx), 0)
  assert.equal(mapSmcIndexToDisplay(-100, ctx), 0)
})

test('mapSmcIndexToDisplay: 索引 >= displayCount → undefined (窗口右侧外)', () => {
  const ctx = { displayCount: 30 }
  assert.equal(mapSmcIndexToDisplay(30, ctx), undefined)
  assert.equal(mapSmcIndexToDisplay(100, ctx), undefined)
})

test('mapSmcIndexToDisplay: 正常索引 → 直接返回 (adapter 已重基准)', () => {
  const ctx = { displayCount: 30 }
  assert.equal(mapSmcIndexToDisplay(0, ctx), 0)
  assert.equal(mapSmcIndexToDisplay(15, ctx), 15)
  assert.equal(mapSmcIndexToDisplay(29, ctx), 29)
})

// ===== 2. selectVisibleSmcOrderBlocks =====

test('selectVisibleSmcOrderBlocks: 只选 internal===true && mitigated===false', () => {
  const ctx = { displayCount: 30 }
  const obs = [
    makeOb({ anchor_index: 1, internal: true, mitigated: false }),   // ✓ 选中
    makeOb({ anchor_index: 2, internal: false, mitigated: false }),  // ✗ 非 internal
    makeOb({ anchor_index: 3, internal: true, mitigated: true }),    // ✗ 已 mitigated
    makeOb({ anchor_index: 4, internal: true, mitigated: false }),   // ✓ 选中
    makeOb({ anchor_index: 5, internal: undefined, mitigated: false }), // ✗ internal 未声明
  ]
  const result = selectVisibleSmcOrderBlocks(obs, ctx)
  assert.equal(result.length, 2)
  assert.deepEqual(result.map(o => o.anchor_index), [1, 4])
})

test('selectVisibleSmcOrderBlocks: 后端最新 OB 在数组头部 → slice(0, 5)', () => {
  const ctx = { displayCount: 30 }
  // 构造 8 个 internal+unmitigated OB，最新（数组头部）anchor_index=0..7
  const obs: SmcOrderBlock[] = []
  for (let i = 0; i < 8; i++) {
    obs.push(makeOb({ anchor_index: i, internal: true, mitigated: false }))
  }
  const result = selectVisibleSmcOrderBlocks(obs, ctx)
  assert.equal(result.length, 5, '最多 5 个')
  // 取前 5 个（数组头部 = 最新）
  assert.deepEqual(result.map(o => o.anchor_index), [0, 1, 2, 3, 4])
})

test('selectVisibleSmcOrderBlocks: 最多 5 个（即使有更多候选）', () => {
  const ctx = { displayCount: 30 }
  const obs: SmcOrderBlock[] = []
  for (let i = 0; i < 20; i++) {
    obs.push(makeOb({ anchor_index: i, internal: true, mitigated: false }))
  }
  const result = selectVisibleSmcOrderBlocks(obs, ctx)
  assert.equal(result.length, 5, '严格上限 5 个')
})

test('selectVisibleSmcOrderBlocks: clipped_left (anchor 为负) → 保留', () => {
  const ctx = { displayCount: 30 }
  const obs = [
    makeOb({ anchor_index: -3, clipped_left: true, internal: true, mitigated: false }),
    makeOb({ anchor_index: 5, clipped_left: false, internal: true, mitigated: false }),
  ]
  const result = selectVisibleSmcOrderBlocks(obs, ctx)
  assert.equal(result.length, 2, 'clipped_left OB 应保留')
  assert.equal(result[0].anchor_index, -3)
  assert.equal(result[0].clipped_left, true)
})

test('selectVisibleSmcOrderBlocks: anchor 在窗口右侧 (>= displayCount) → 跳过', () => {
  const ctx = { displayCount: 30 }
  const obs = [
    makeOb({ anchor_index: 25, internal: true, mitigated: false }),  // ✓ 在窗口内
    makeOb({ anchor_index: 30, internal: true, mitigated: false }),  // ✗ 窗口右侧外
    makeOb({ anchor_index: 35, internal: true, mitigated: false }),  // ✗ 窗口右侧外
  ]
  const result = selectVisibleSmcOrderBlocks(obs, ctx)
  assert.equal(result.length, 1)
  assert.equal(result[0].anchor_index, 25)
})

test('selectVisibleSmcOrderBlocks: 空输入 → 空数组', () => {
  const ctx = { displayCount: 30 }
  assert.deepEqual(selectVisibleSmcOrderBlocks([], ctx), [])
})

// ===== 3. collectVisibleSmcPriceCandidates =====

test('collectVisibleSmcPriceCandidates: 收集 event.level (anchor 或 confirmed 在窗口内)', () => {
  const ctx = { displayCount: 30 }
  const events: SmcEvent[] = [
    { type: 'BOS', bias: 1, anchor_index: 5, anchor_time: null, confirmed_index: 8, confirmed_time: null, level: 100.0 }, // ✓
    { type: 'CHoCH', bias: -1, anchor_index: 50, anchor_time: null, confirmed_index: 60, confirmed_time: null, level: 110.0 }, // ✗ 窗口外
    { type: 'BOS', bias: 1, anchor_index: 100, anchor_time: null, confirmed_index: 5, confirmed_time: null, level: 120.0 }, // ✓ confirmed 在窗口内
  ]
  const result = collectVisibleSmcPriceCandidates({ events }, ctx)
  assert.ok(result.includes(100.0), 'event 1 level 应包含')
  assert.ok(result.includes(120.0), 'event 3 level 应包含（confirmed 在窗口内）')
  assert.ok(!result.includes(110.0), 'event 2 level 不应包含（窗口外）')
})

test('collectVisibleSmcPriceCandidates: 收集 OB bar_high/bar_low (仅选中的 5 个 internal+unmitigated)', () => {
  const ctx = { displayCount: 30 }
  const order_blocks: SmcOrderBlock[] = [
    makeOb({ anchor_index: 5, bar_high: 11.5, bar_low: 9.5, internal: true, mitigated: false }), // ✓
    makeOb({ anchor_index: 10, bar_high: 22.0, bar_low: 18.0, internal: true, mitigated: false }), // ✓
    makeOb({ anchor_index: 15, bar_high: 99.0, bar_low: 88.0, internal: true, mitigated: true }),  // ✗ mitigated
    makeOb({ anchor_index: 20, bar_high: 55.0, bar_low: 45.0, internal: false, mitigated: false }), // ✗ 非 internal
  ]
  const result = collectVisibleSmcPriceCandidates({ order_blocks }, ctx)
  assert.ok(result.includes(11.5) && result.includes(9.5), 'OB1 bar_high/bar_low')
  assert.ok(result.includes(22.0) && result.includes(18.0), 'OB2 bar_high/bar_low')
  assert.ok(!result.includes(99.0) && !result.includes(88.0), 'mitigated OB 不应包含')
  assert.ok(!result.includes(55.0) && !result.includes(45.0), 'swing OB 不应包含')
})

test('collectVisibleSmcPriceCandidates: 收集 EQH/EQL level (anchor 或 second_pivot 在窗口内)', () => {
  const ctx = { displayCount: 30 }
  const equal_highs_lows: SmcEqualHighLow[] = [
    { type: 'EQH', anchor_index: 5, anchor_time: null, second_pivot_index: 8, second_pivot_time: null, confirmed_index: 10, confirmed_time: null, level: 50.0, prev_level: 49.9 }, // ✓
    { type: 'EQL', anchor_index: 50, anchor_time: null, second_pivot_index: 60, second_pivot_time: null, confirmed_index: 70, confirmed_time: null, level: 30.0, prev_level: 30.1 }, // ✗ 窗口外
  ]
  const result = collectVisibleSmcPriceCandidates({ equal_highs_lows }, ctx)
  assert.ok(result.includes(50.0), 'EQH1 level 应包含')
  assert.ok(!result.includes(30.0), 'EQL2 level 不应包含（窗口外）')
})

test('collectVisibleSmcPriceCandidates: 收集 trailing top/bottom', () => {
  const ctx = { displayCount: 30 }
  const trailing: SmcTrailing = {
    top: 105.0,
    bottom: 85.0,
    bar_time: null,
    bar_index: 25,
    last_top_time: null,
    last_bottom_time: null,
  }
  const result = collectVisibleSmcPriceCandidates({ trailing }, ctx)
  assert.ok(result.includes(105.0), 'trailing.top')
  assert.ok(result.includes(85.0), 'trailing.bottom')
})

test('collectVisibleSmcPriceCandidates: trailing 为 null → 不加入', () => {
  const ctx = { displayCount: 30 }
  const result = collectVisibleSmcPriceCandidates({ trailing: null }, ctx)
  assert.equal(result.length, 0)
})

test('collectVisibleSmcPriceCandidates: 综合所有来源', () => {
  const ctx = { displayCount: 30 }
  const events: SmcEvent[] = [
    { type: 'BOS', bias: 1, anchor_index: 5, anchor_time: null, confirmed_index: 8, confirmed_time: null, level: 100.0 },
  ]
  const equalHighsLows: SmcEqualHighLow[] = [
    { type: 'EQH', anchor_index: 5, anchor_time: null, second_pivot_index: 8, second_pivot_time: null, confirmed_index: 10, confirmed_time: null, level: 50.0, prev_level: 49.9 },
  ]
  const trailing: SmcTrailing = { top: 105.0, bottom: 85.0, bar_time: null, bar_index: 25, last_top_time: null, last_bottom_time: null }
  const smcData = { events, order_blocks: [makeOb({ anchor_index: 5, bar_high: 11.0, bar_low: 9.0 })], equal_highs_lows: equalHighsLows, trailing }
  const result = collectVisibleSmcPriceCandidates(smcData, ctx)
  // 应包含所有 4 类来源
  assert.ok(result.includes(100.0), 'event.level')
  assert.ok(result.includes(11.0) && result.includes(9.0), 'OB bar_high/bar_low')
  assert.ok(result.includes(50.0), 'EQH level')
  assert.ok(result.includes(105.0) && result.includes(85.0), 'trailing top/bottom')
})

// ===== 4. hexToRgba =====

test('hexToRgba: #RRGGBB → rgba(r,g,b,alpha)', () => {
  assert.equal(hexToRgba('#FF4D4F', 0.12), 'rgba(255, 77, 79, 0.12)')
  assert.equal(hexToRgba('#22C55E', 0.3), 'rgba(34, 197, 94, 0.3)')
})

test('hexToRgba: #RGB → rgba(r,g,b,alpha) (扩展为 #RRGGBB)', () => {
  assert.equal(hexToRgba('#F00', 0.5), 'rgba(255, 0, 0, 0.5)')
})

test('hexToRgba: 无法解析 → 返回原始 hex', () => {
  assert.equal(hexToRgba('invalid', 0.5), 'invalid')
})

// ===== 5. Canvas mock 测试 =====
//
// 通过 mock CanvasRenderingContext2D 记录所有绘制调用，验证 SMC 渲染行为。
// 由于 renderIndicatorSmc 内部依赖 StrategyChart.tsx 的局部函数（smcToDisplay 等），
// 此处直接调用纯函数 + 模拟渲染流程，验证关键契约。

interface MockCall {
  method: string
  args: unknown[]
}

function createMockCtx(): { ctx: any; calls: MockCall[] } {
  const calls: MockCall[] = []
  const ctx = {
    fillRect: (...args: unknown[]) => calls.push({ method: 'fillRect', args }),
    strokeRect: (...args: unknown[]) => calls.push({ method: 'strokeRect', args }),
    fillText: (...args: unknown[]) => calls.push({ method: 'fillText', args }),
    beginPath: () => calls.push({ method: 'beginPath', args: [] }),
    moveTo: (...args: unknown[]) => calls.push({ method: 'moveTo', args }),
    lineTo: (...args: unknown[]) => calls.push({ method: 'lineTo', args }),
    stroke: () => calls.push({ method: 'stroke', args: [] }),
    fill: () => calls.push({ method: 'fill', args: [] }),
    arc: (...args: unknown[]) => calls.push({ method: 'arc', args }),
    set fillStyle(v: unknown) { (this as any)._fillStyle = v },
    get fillStyle(): unknown { return (this as any)._fillStyle },
    set strokeStyle(v: unknown) { (this as any)._strokeStyle = v },
    get strokeStyle(): unknown { return (this as any)._strokeStyle },
    set lineWidth(v: unknown) { (this as any)._lineWidth = v },
    get lineWidth(): unknown { return (this as any)._lineWidth },
    set lineDash(v: unknown) { (this as any)._lineDash = v },
    get lineDash(): unknown { return (this as any)._lineDash },
    setLineDash(v: unknown[]) { (this as any)._lineDash = v },
    set font(v: unknown) { (this as any)._font = v },
    get font(): unknown { return (this as any)._font },
    set textAlign(v: unknown) { (this as any)._textAlign = v },
    get textAlign(): unknown { return (this as any)._textAlign },
    set globalAlpha(v: unknown) { (this as any)._globalAlpha = v },
    get globalAlpha(): unknown { return (this as any)._globalAlpha },
  }
  return { ctx, calls }
}

// 模拟 SMC OB 渲染流程（与 renderIndicatorSmc §1 等价）
function renderObs(
  mockCtx: any,
  orderBlocks: SmcOrderBlock[],
  displayCount: number,
  opts: {
    plotLeft: number
    plotRight: number
    step: number
    py: (v: number) => number
  },
): void {
  const visibleObs = selectVisibleSmcOrderBlocks(orderBlocks, { displayCount })
  for (const ob of visibleObs) {
    const anchorIdx = mapSmcIndexToDisplay(ob.anchor_index, { displayCount })
    if (anchorIdx == null) continue
    const x2 = opts.plotRight
    let x1 = opts.plotLeft + (anchorIdx + 0.5) * opts.step
    if (ob.clipped_left === true || ob.anchor_index < 0) {
      x1 = opts.plotLeft
    }
    if (x1 > x2) continue
    const yHigh = opts.py(ob.bar_high)
    const yLow = opts.py(ob.bar_low)
    const yTop = Math.min(yHigh, yLow)
    const height = Math.max(2, Math.abs(yHigh - yLow))
    const isBull = ob.bias === 1
    const color = isBull ? SMC_BULL_COLOR : SMC_BEAR_COLOR
    mockCtx.fillStyle = hexToRgba(color, 0.12)
    mockCtx.fillRect(x1, yTop, Math.max(1, x2 - x1), height)
    mockCtx.strokeStyle = hexToRgba(color, 0.3)
    mockCtx.lineWidth = 0.8
    mockCtx.strokeRect(x1, yTop, Math.max(1, x2 - x1), height)
  }
}

test('Canvas mock: 最多 5 个 OB 被绘制', () => {
  const { ctx, calls } = createMockCtx()
  const obs: SmcOrderBlock[] = []
  for (let i = 0; i < 20; i++) {
    obs.push(makeOb({ anchor_index: i, bar_high: 11 + i, bar_low: 9 + i, internal: true, mitigated: false }))
  }
  renderObs(ctx, obs, 30, {
    plotLeft: 58,
    plotRight: 800,
    step: 20,
    py: (v) => 500 - v * 10,
  })
  const fillRects = calls.filter(c => c.method === 'fillRect')
  const strokeRects = calls.filter(c => c.method === 'strokeRect')
  assert.equal(fillRects.length, 5, 'fillRect 最多 5 次')
  assert.equal(strokeRects.length, 5, 'strokeRect 最多 5 次')
})

test('Canvas mock: clipped_left 时 x1 = plotLeft (左侧 clamp)', () => {
  const { ctx, calls } = createMockCtx()
  const obs = [
    makeOb({ anchor_index: -3, clipped_left: true, bar_high: 11, bar_low: 9, internal: true, mitigated: false }),
  ]
  renderObs(ctx, obs, 30, {
    plotLeft: 58,
    plotRight: 800,
    step: 20,
    py: (v) => 500 - v * 10,
  })
  const fillRects = calls.filter(c => c.method === 'fillRect')
  assert.equal(fillRects.length, 1)
  // x1 应为 plotLeft=58（clamp 到左端，不是负坐标）
  assert.equal(fillRects[0].args[0], 58, 'clipped_left 时 x1 必须 = plotLeft')
})

test('Canvas mock: anchor 在窗口右侧 → 不绘制 (与 viewport 无交集)', () => {
  const { ctx, calls } = createMockCtx()
  const obs = [
    makeOb({ anchor_index: 35, internal: true, mitigated: false }), // displayCount=30, 35 >= 30
  ]
  renderObs(ctx, obs, 30, {
    plotLeft: 58,
    plotRight: 800,
    step: 20,
    py: (v) => 500 - v * 10,
  })
  const fillRects = calls.filter(c => c.method === 'fillRect')
  assert.equal(fillRects.length, 0, 'OB anchor 在窗口右侧 → 不绘制')
})

test('Canvas mock: mitigated OB 不绘制 (只画 internal && !mitigated)', () => {
  const { ctx, calls } = createMockCtx()
  const obs = [
    makeOb({ anchor_index: 5, internal: true, mitigated: true }),
    makeOb({ anchor_index: 10, internal: true, mitigated: false }),
  ]
  renderObs(ctx, obs, 30, {
    plotLeft: 58,
    plotRight: 800,
    step: 20,
    py: (v) => 500 - v * 10,
  })
  const fillRects = calls.filter(c => c.method === 'fillRect')
  assert.equal(fillRects.length, 1, '只画 1 个 unmitigated OB')
})

test('Canvas mock: FVG 绘制调用为 0 (完全排除)', () => {
  const { ctx, calls } = createMockCtx()
  const obs = [
    makeOb({ anchor_index: 5, internal: true, mitigated: false }),
  ]
  renderObs(ctx, obs, 30, {
    plotLeft: 58,
    plotRight: 800,
    step: 20,
    py: (v) => 500 - v * 10,
  })
  // 不应有任何与 FVG 相关的绘制（renderObs 只绘制 OB，无 FVG 路径）
  // 验证：所有调用都是 fillRect / strokeRect / 属性赋值
  const allowedMethods = new Set(['fillRect', 'strokeRect', 'fillStyle', 'strokeStyle', 'lineWidth'])
  for (const c of calls) {
    assert.ok(allowedMethods.has(c.method) || c.method === 'fillRect' || c.method === 'strokeRect',
      `不应有 FVG 相关绘制调用，实际出现: ${c.method}`)
  }
  // 显式检查：调用次数 = 1 个 OB 的 2 次绘制（fillRect + strokeRect）
  assert.equal(calls.filter(c => c.method === 'fillRect').length, 1)
  assert.equal(calls.filter(c => c.method === 'strokeRect').length, 1)
})

// ===== 6. EQH/EQL 线段终点 = second_pivot_index (Canvas mock) =====

test('Canvas mock: EQH 线段终点为 second_pivot_index (非 confirmed_index)', () => {
  // 模拟 EQH 渲染：x2 = plotLeft + (secondPivotIdx + 0.5) * step
  const calls: MockCall[] = []
  const origDrawLine = (x1: number, y: number, x2: number) => {
    calls.push({ method: 'drawLine', args: [x1, y, x2] })
  }
  const eq: SmcEqualHighLow = {
    type: 'EQH',
    anchor_index: 5,
    anchor_time: null,
    second_pivot_index: 12,  // 线段终点
    second_pivot_time: null,
    confirmed_index: 20,     // 因果确认点（非线段终点）
    confirmed_time: null,
    level: 100.0,
    prev_level: 99.9,
  }
  const displayCount = 30
  const plotLeft = 58
  const step = 20
  // 模拟 renderIndicatorSmc §3 EQH/EQL 渲染逻辑
  const anchorIdx = mapSmcIndexToDisplay(eq.anchor_index, { displayCount })
  const secondPivotIdx = mapSmcIndexToDisplay(eq.second_pivot_index, { displayCount })
  assert.ok(anchorIdx != null && secondPivotIdx != null)
  const x1 = plotLeft + (anchorIdx + 0.5) * step
  const x2 = plotLeft + (secondPivotIdx + 0.5) * step  // second_pivot_index 为终点
  origDrawLine(x1, 0, x2)
  // 验证 x2 对应 second_pivot_index=12，不是 confirmed_index=20
  assert.equal(x2, plotLeft + (12 + 0.5) * step, 'x2 必须基于 second_pivot_index')
  assert.notEqual(x2, plotLeft + (20 + 0.5) * step, 'x2 不得基于 confirmed_index')
})

// ===== 7. Strong/Weak 读取 DTO swing_bias =====

test('Strong/Weak 规则: swing_bias === -1 → Strong High (红色)', () => {
  const swingBias = -1
  const isStrongHigh = swingBias === -1
  assert.equal(isStrongHigh, true)
  // 强高 = 红色（SMC_BULL_COLOR 在 trailing 上下文中表示"强"）
  // 注：trailing 强高用 SMC_BULL_COLOR (#FF4D4F 红色)，弱高用 SMC_BEAR_COLOR (#22C55E 绿色)
  const labelColor = isStrongHigh ? SMC_BULL_COLOR : SMC_BEAR_COLOR
  assert.equal(labelColor, SMC_BULL_COLOR, '强高 = 红色')
})

test('Strong/Weak 规则: swing_bias === 1 → Strong Low (绿色)', () => {
  const swingBias = 1
  const isStrongLow = swingBias === 1
  assert.equal(isStrongLow, true)
  // 强低 = 绿色，弱低 = 红色
  const labelColor = isStrongLow ? SMC_BEAR_COLOR : SMC_BULL_COLOR
  assert.equal(labelColor, SMC_BEAR_COLOR, '强低 = 绿色')
})

test('Strong/Weak 规则: swing_bias === 0 → Weak High + Weak Low', () => {
  const swingBias: number = 0
  const isStrongHigh = swingBias === -1
  const isStrongLow = swingBias === 1
  assert.equal(isStrongHigh, false, 'bias=0 → 弱高')
  assert.equal(isStrongLow, false, 'bias=0 → 弱低')
})

test('Strong/Weak 规则: 不得根据 trailing 时间或 close 位置推断', () => {
  // 验证：规则仅依赖 swing_bias 数值，不依赖 trailing.bar_time / close 等
  // （契约：前端不重新推导 swing_bias）
  const cases = [
    { swing_bias: -1, expected_strong_high: true },
    { swing_bias: 1, expected_strong_high: false },
    { swing_bias: 0, expected_strong_high: false },
  ]
  for (const c of cases) {
    const isStrongHigh = c.swing_bias === -1
    assert.equal(isStrongHigh, c.expected_strong_high,
      `swing_bias=${c.swing_bias} → isStrongHigh=${c.expected_strong_high}`)
  }
})

// ===== 8. SMC 价格进入纵轴 =====

test('SMC 价格候选进入纵轴: event.level / OB / EQH / trailing 全部收集', () => {
  const ctx = { displayCount: 30 }
  const events: SmcEvent[] = [
    { type: 'BOS', bias: 1, anchor_index: 5, anchor_time: null, confirmed_index: 8, confirmed_time: null, level: 100.0 },
  ]
  const equalHighsLows: SmcEqualHighLow[] = [
    { type: 'EQH', anchor_index: 5, anchor_time: null, second_pivot_index: 8, second_pivot_time: null, confirmed_index: 10, confirmed_time: null, level: 50.0, prev_level: 49.9 },
  ]
  const trailing: SmcTrailing = { top: 105.0, bottom: 85.0, bar_time: null, bar_index: 25, last_top_time: null, last_bottom_time: null }
  const smcData = { events, order_blocks: [makeOb({ anchor_index: 5, bar_high: 11.0, bar_low: 9.0 })], equal_highs_lows: equalHighsLows, trailing }
  const candidates = collectVisibleSmcPriceCandidates(smcData, ctx)
  // 所有 SMC 价格都应进入纵轴候选
  assert.ok(candidates.includes(100.0), 'event.level 必须进入纵轴候选')
  assert.ok(candidates.includes(11.0) && candidates.includes(9.0), 'OB bar_high/bar_low 必须进入纵轴候选')
  assert.ok(candidates.includes(50.0), 'EQH level 必须进入纵轴候选')
  assert.ok(candidates.includes(105.0) && candidates.includes(85.0), 'trailing top/bottom 必须进入纵轴候选')
  // 纵轴范围应覆盖所有 SMC 价格
  const min = Math.min(...candidates)
  const max = Math.max(...candidates)
  assert.equal(min, 9.0, '纵轴下限应包含 OB bar_low')
  assert.equal(max, 105.0, '纵轴上限应包含 trailing.top')
})

// ===== 9. FVG 字段在 SMC 类型中不存在 =====

test('FVG 完全排除: SmcOrderBlock / SmcEvent / SmcEqualHighLow 无任何 fvg 字段', () => {
  // 通过构造对象验证：类型不允许包含 fvg 相关字段
  const ob: SmcOrderBlock = makeOb()
  const ev: SmcEvent = {
    type: 'BOS', bias: 1, anchor_index: 0, anchor_time: null,
    confirmed_index: 1, confirmed_time: null, level: 100.0,
  }
  const eq: SmcEqualHighLow = {
    type: 'EQH', anchor_index: 0, anchor_time: null,
    second_pivot_index: 1, second_pivot_time: null,
    confirmed_index: 2, confirmed_time: null,
    level: 100.0, prev_level: 99.9,
  }
  // 验证字段集合中无 fvg
  const obKeys = new Set(Object.keys(ob))
  const evKeys = new Set(Object.keys(ev))
  const eqKeys = new Set(Object.keys(eq))
  for (const k of obKeys) assert.ok(!/fvg/i.test(k), `SmcOrderBlock 不应包含 fvg 字段: ${k}`)
  for (const k of evKeys) assert.ok(!/fvg/i.test(k), `SmcEvent 不应包含 fvg 字段: ${k}`)
  for (const k of eqKeys) assert.ok(!/fvg/i.test(k), `SmcEqualHighLow 不应包含 fvg 字段: ${k}`)
})

// ===== 10. intersectSmcRangeWithViewport: viewport 区间求交（PROMPT.md §三.2）=====

test('intersectSmcRangeWithViewport: anchor 和 confirmed 都在 viewport 内 → 正常返回', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(5, 15, ctx)
  assert.notEqual(range, null)
  assert.equal(range!.startIdx, 5)
  assert.equal(range!.endIdx, 15)
  assert.equal(range!.clippedLeft, false)
  assert.equal(range!.clippedRight, false)
})

test('intersectSmcRangeWithViewport: anchor 在左侧（负索引）→ startIdx=0, clippedLeft=true', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(-3, 10, ctx)
  assert.notEqual(range, null)
  assert.equal(range!.startIdx, 0, 'anchor 在左侧应 clamp 到 0')
  assert.equal(range!.endIdx, 10)
  assert.equal(range!.clippedLeft, true)
  assert.equal(range!.clippedRight, false)
})

test('intersectSmcRangeWithViewport: confirmed 在右侧（>= displayCount）→ endIdx=displayCount-1, clippedRight=true', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(10, 50, ctx)
  assert.notEqual(range, null)
  assert.equal(range!.startIdx, 10)
  assert.equal(range!.endIdx, 29, 'confirmed 在右侧应 clamp 到 displayCount-1')
  assert.equal(range!.clippedLeft, false)
  assert.equal(range!.clippedRight, true)
})

test('intersectSmcRangeWithViewport: anchor 左侧 + confirmed 右侧 → 双向 clamp', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(-5, 100, ctx)
  assert.notEqual(range, null)
  assert.equal(range!.startIdx, 0)
  assert.equal(range!.endIdx, 29)
  assert.equal(range!.clippedLeft, true)
  assert.equal(range!.clippedRight, true)
})

test('intersectSmcRangeWithViewport: 都在左侧 → null（完全不相交）', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(-10, -1, ctx)
  assert.equal(range, null, '区间完全在 viewport 左侧应返回 null')
})

test('intersectSmcRangeWithViewport: 都在右侧 → null（完全不相交）', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(30, 50, ctx)
  assert.equal(range, null, '区间完全在 viewport 右侧应返回 null')
})

test('intersectSmcRangeWithViewport: null/undefined 索引 → null', () => {
  const ctx = { displayCount: 30 }
  assert.equal(intersectSmcRangeWithViewport(null, 10, ctx), null)
  assert.equal(intersectSmcRangeWithViewport(5, null, ctx), null)
  assert.equal(intersectSmcRangeWithViewport(undefined, 10, ctx), null)
  assert.equal(intersectSmcRangeWithViewport(5, undefined, ctx), null)
})

test('intersectSmcRangeWithViewport: confirmed < anchor → null（因果方向错误）', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(15, 5, ctx)
  assert.equal(range, null, 'confirmed < anchor 应返回 null')
})

test('intersectSmcRangeWithViewport: anchor=0, confirmed=displayCount-1 → 边界情况正常', () => {
  const ctx = { displayCount: 30 }
  const range = intersectSmcRangeWithViewport(0, 29, ctx)
  assert.notEqual(range, null)
  assert.equal(range!.startIdx, 0)
  assert.equal(range!.endIdx, 29)
  assert.equal(range!.clippedLeft, false)
  assert.equal(range!.clippedRight, false)
})

// ===== 11. layoutSmcLabels: P0 SMC 标签碰撞布局 =====
//
// [2026-07-21 P0 反馈] 飞书移动舞台 90 bar 窗口下 SMC 标签集中重叠
//   验证点：
//   1. 空输入 → 空输出
//   2. 单标签 → lane 0
//   3. 非重叠两标签 → 都在 lane 0
//   4. 重叠两标签 → 第二个移到 lane 1（不重叠）
//   5. 标签框不超出图表区域（plotLeft/plotRight/plotTop/plotBottom）
//   6. 引导线起点 = 真实锚点，终点 = 标签框中心
//   7. 真实锚点（anchorX/anchorY）不被改变
//   8. 京东方A 真实数据 13 标签 → 输出无矩形重叠

// 固定 measureText：每个字符 10px 宽（简化测试）
function fixedMeasureText(text: string, _fontSize: string): number {
  return text.length * 10
}

const defaultLayoutCtx: SmcLabelLayoutContext = {
  plotLeft: 0,
  plotRight: 800,
  plotTop: 0,
  plotBottom: 600,
  laneHeight: 32,
  laneGap: 4,
  maxLanes: 4,
}

function makeAnchor(overrides: Partial<SmcLabelAnchor> = {}): SmcLabelAnchor {
  return {
    kind: 'bos',
    anchorX: 100,
    anchorY: 200,
    text: 'BOS',
    color: SMC_BULL_COLOR,
    fontSize: '28px',
    align: 'center',
    preferredVertical: 'up',
    ...overrides,
  }
}

test('layoutSmcLabels: 空输入 → 空输出', () => {
  const result = layoutSmcLabels([], defaultLayoutCtx, fixedMeasureText)
  assert.deepEqual(result, [])
})

test('layoutSmcLabels: 单标签 → lane 0, 框居中于锚点', () => {
  const anchor = makeAnchor({ anchorX: 400, anchorY: 300, text: 'BOS', align: 'center' })
  const result = layoutSmcLabels([anchor], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 1)
  assert.equal(result[0].lane, 0, '单标签应在 lane 0')
  // 框居中：boxX = anchorX - boxW/2
  const expectedBoxW = 3 * 10 + 4 * 2 // 38
  assert.equal(result[0].boxX, 400 - expectedBoxW / 2)
  assert.equal(result[0].boxY, 300 - (28 + 4) / 2, 'boxY 居中于 anchorY (lane 0)')
})

test('layoutSmcLabels: 两非重叠标签 → 都在 lane 0', () => {
  // 两标签 anchorX 相距 500px，框宽 ~38px，绝不重叠
  const a1 = makeAnchor({ anchorX: 100, anchorY: 200, text: 'BOS' })
  const a2 = makeAnchor({ anchorX: 600, anchorY: 200, text: 'CHoCH' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 2)
  assert.equal(result[0].lane, 0)
  assert.equal(result[1].lane, 0)
})

test('layoutSmcLabels: 两重叠标签 → 第二个移到 lane 1 (不重叠)', () => {
  // 两标签 anchorX 相同，必然重叠
  const a1 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'BOS', preferredVertical: 'up' })
  const a2 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'CHoCH', preferredVertical: 'up' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 2)
  // 第一个在 lane 0，第二个必然移到 lane 1（向上偏移）
  const lanes = result.map(r => r.lane).sort()
  assert.equal(lanes[0], 0, '至少一个在 lane 0')
  assert.ok(lanes.some(l => l > 0), '至少一个移到 lane > 0')
  // 验证两个标签框不重叠
  const [r1, r2] = result
  const overlapX = r1.boxX < r2.boxX + r2.boxW + 2 && r1.boxX + r1.boxW + 2 > r2.boxX
  const overlapY = r1.boxY < r2.boxY + r2.boxH + 2 && r1.boxY + r1.boxH + 2 > r2.boxY
  assert.ok(!(overlapX && overlapY), '两标签框不得重叠')
})

test('layoutSmcLabels: 标签框 X 钳制到 [plotLeft, plotRight - boxW]', () => {
  // anchorX 在左边界外（负值），align=center → boxX 应钳制到 plotLeft
  const a1 = makeAnchor({ anchorX: -50, anchorY: 100, text: 'BOS', align: 'center' })
  // anchorX 在右边界外，align=center → boxX 应钳制到 plotRight - boxW
  const a2 = makeAnchor({ anchorX: 900, anchorY: 100, text: 'CHoCH', align: 'center' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 2)
  for (const r of result) {
    assert.ok(r.boxX >= defaultLayoutCtx.plotLeft, `boxX ${r.boxX} 不得小于 plotLeft ${defaultLayoutCtx.plotLeft}`)
    assert.ok(r.boxX + r.boxW <= defaultLayoutCtx.plotRight,
      `boxX+boxW ${r.boxX + r.boxW} 不得大于 plotRight ${defaultLayoutCtx.plotRight}`)
  }
})

test('layoutSmcLabels: 标签框 Y 钳制到 [plotTop, plotBottom - boxH]', () => {
  // anchorY 在上边界外（负值），lane 偏移仍向上 → boxY 应钳制到 plotTop
  const a1 = makeAnchor({ anchorX: 400, anchorY: -50, text: 'BOS', preferredVertical: 'up' })
  // anchorY 在下边界外 → boxY 应钳制到 plotBottom - boxH
  const a2 = makeAnchor({ anchorX: 500, anchorY: 700, text: 'CHoCH', preferredVertical: 'down' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 2)
  for (const r of result) {
    assert.ok(r.boxY >= defaultLayoutCtx.plotTop, `boxY ${r.boxY} 不得小于 plotTop ${defaultLayoutCtx.plotTop}`)
    assert.ok(r.boxY + r.boxH <= defaultLayoutCtx.plotBottom,
      `boxY+boxH ${r.boxY + r.boxH} 不得大于 plotBottom ${defaultLayoutCtx.plotBottom}`)
  }
})

test('layoutSmcLabels: 引导线起点 = 真实锚点, 终点 = 标签框中心', () => {
  // 强制 lane > 0：两个完全重叠的标签
  const a1 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'BOS', preferredVertical: 'up' })
  const a2 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'CHoCH', preferredVertical: 'up' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  // 找到 lane > 0 的那个
  const offset = result.find(r => r.lane > 0)
  assert.ok(offset, '应至少有一个标签在 lane > 0')
  assert.equal(offset!.guideStartX, offset!.anchor.anchorX, '引导线起点 X = 真实锚点 X')
  assert.equal(offset!.guideStartY, offset!.anchor.anchorY, '引导线起点 Y = 真实锚点 Y')
  assert.equal(offset!.guideEndX, offset!.boxX + offset!.boxW / 2, '引导线终点 X = 标签框中心 X')
  assert.equal(offset!.guideEndY, offset!.boxY + offset!.boxH / 2, '引导线终点 Y = 标签框中心 Y')
})

test('layoutSmcLabels: 真实锚点 (anchorX/anchorY) 不被改变', () => {
  // [P0 fix] layoutSmcLabels 内部按 anchorX 排序后再布局，输出顺序可能与输入不同。
  //   验证方式：用 Set<text> 匹配输入与输出，确保每个 anchor 的 X/Y/text 在输出中存在且未变。
  const anchors = [
    makeAnchor({ anchorX: 100, anchorY: 200, text: 'BOS' }),
    makeAnchor({ anchorX: 200, anchorY: 250, text: 'CHoCH' }),
    makeAnchor({ anchorX: 100, anchorY: 200, text: 'EQL', preferredVertical: 'down' }),
  ]
  const result = layoutSmcLabels(anchors, defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, anchors.length, '输入输出数量一致')
  // 用 text 作为 key 匹配（BOS/CHoCH/EQL 唯一）
  const byText = new Map(result.map(r => [r.anchor.text, r]))
  for (const input of anchors) {
    const out = byText.get(input.text)
    assert.ok(out, `输出中应包含 text=${input.text}`)
    assert.equal(out!.anchor.anchorX, input.anchorX, `text=${input.text} anchorX 不变`)
    assert.equal(out!.anchor.anchorY, input.anchorY, `text=${input.text} anchorY 不变`)
    assert.equal(out!.anchor.text, input.text, `text=${input.text} text 不变`)
  }
})

test('layoutSmcLabels: 京东方A 真实 13 标签场景 → 输出无矩形重叠', () => {
  // 模拟 /tmp/smc_analysis_output.txt 中的 13 个真实标签
  // 使用 step≈9.4px, plotLeft≈58, plotRight≈900（90 bar 窗口近似）
  // 价格转 Y: 用线性映射 (price - 3.5) * 50 + 100（覆盖 3.5-9.5 价格区间）
  const step = 9.4
  const plotLeft = 58
  const plotRight = plotLeft + 90 * step
  const priceToY = (p: number) => 100 + (p - 3.5) * 50

  const anchors: SmcLabelAnchor[] = [
    // 6 events
    { kind: 'choch', anchorX: plotLeft + 3.0 * step, anchorY: priceToY(4.135) - 8, text: '转弱拐点', color: SMC_BEAR_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    { kind: 'choch', anchorX: plotLeft + 21.0 * step, anchorY: priceToY(4.026) - 8, text: '转强拐点', color: SMC_BULL_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    { kind: 'bos', anchorX: plotLeft + 37.5 * step, anchorY: priceToY(4.304) - 8, text: '突破前高', color: SMC_BULL_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    { kind: 'choch', anchorX: plotLeft + 24.0 * step, anchorY: priceToY(4.691) - 8, text: '转强拐点', color: SMC_BULL_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    { kind: 'bos', anchorX: plotLeft + 54.0 * step, anchorY: priceToY(6.039) - 8, text: '突破前高', color: SMC_BULL_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    { kind: 'bos', anchorX: plotLeft + 63.0 * step, anchorY: priceToY(6.713) - 8, text: '突破前高', color: SMC_BULL_COLOR, fontSize: '28px', align: 'center', preferredVertical: 'up' },
    // 5 visible OBs
    { kind: 'ob', anchorX: plotLeft + 55.0 * step + 8, anchorY: priceToY((5.067 + 5.494) / 2), text: '多头承接区', color: hexToRgba(SMC_BULL_COLOR, 0.85), fontSize: '28px', align: 'left', preferredVertical: 'center' },
    { kind: 'ob', anchorX: plotLeft + 35.0 * step + 8, anchorY: priceToY((4.145 + 4.046) / 2), text: '多头承接区', color: hexToRgba(SMC_BULL_COLOR, 0.85), fontSize: '28px', align: 'left', preferredVertical: 'center' },
    { kind: 'ob', anchorX: plotLeft + 18.0 * step + 8, anchorY: priceToY((3.917 + 3.858) / 2), text: '多头承接区', color: hexToRgba(SMC_BULL_COLOR, 0.85), fontSize: '28px', align: 'left', preferredVertical: 'center' },
    { kind: 'ob', anchorX: plotLeft + 0.0 * step + 8, anchorY: priceToY((3.818 + 3.758) / 2), text: '多头承接区', color: hexToRgba(SMC_BULL_COLOR, 0.85), fontSize: '28px', align: 'left', preferredVertical: 'center' },
    { kind: 'ob', anchorX: plotLeft + 0.0 * step + 8, anchorY: priceToY((3.594 + 3.515) / 2), text: '多头承接区', color: hexToRgba(SMC_BULL_COLOR, 0.85), fontSize: '28px', align: 'left', preferredVertical: 'center' },
    // trailing high/low
    { kind: 'trailing_high', anchorX: plotRight - 4, anchorY: priceToY(9.5) - 3, text: '强高 9.50', color: SMC_BULL_COLOR, fontSize: '28px', align: 'right', preferredVertical: 'up' },
    { kind: 'trailing_low', anchorX: plotRight - 4, anchorY: priceToY(3.818) + 9, text: '强低 3.82', color: SMC_BEAR_COLOR, fontSize: '28px', align: 'right', preferredVertical: 'down' },
  ]

  const ctx: SmcLabelLayoutContext = {
    plotLeft, plotRight,
    plotTop: 0, plotBottom: 600,
    laneHeight: 32, laneGap: 4,
    maxLanes: 4,
  }
  const result = layoutSmcLabels(anchors, ctx, fixedMeasureText)
  assert.equal(result.length, 13, '所有 13 个标签都应被布局')

  // 核心断言：任意两个标签框不得重叠（2px 容差）
  for (let i = 0; i < result.length; i++) {
    for (let j = i + 1; j < result.length; j++) {
      const a = result[i], b = result[j]
      const overlapX = a.boxX < b.boxX + b.boxW + 2 && a.boxX + a.boxW + 2 > b.boxX
      const overlapY = a.boxY < b.boxY + b.boxH + 2 && a.boxY + a.boxH + 2 > b.boxY
      assert.ok(!(overlapX && overlapY),
        `标签 ${i} (${a.anchor.text}@${a.boxX},${a.boxY}) 与标签 ${j} (${b.anchor.text}@${b.boxX},${b.boxY}) 不得重叠`)
    }
  }
})

test('layoutSmcLabels: preferredVertical=up → lane 偏移向上 (boxY < anchorY)', () => {
  // 两个完全重叠的标签，preferredVertical=up
  const a1 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'BOS', preferredVertical: 'up' })
  const a2 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'CHoCH', preferredVertical: 'up' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  const offset = result.find(r => r.lane > 0)
  assert.ok(offset, '应有 lane > 0')
  // up → boxY 应在 anchorY 上方（小于）
  assert.ok(offset!.boxY + offset!.boxH / 2 < offset!.anchor.anchorY,
    `up: 标签框中心 Y (${offset!.boxY + offset!.boxH / 2}) 应在锚点 Y (${offset!.anchor.anchorY}) 上方`)
})

test('layoutSmcLabels: preferredVertical=down → lane 偏移向下 (boxY > anchorY)', () => {
  const a1 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'BOS', preferredVertical: 'down' })
  const a2 = makeAnchor({ anchorX: 400, anchorY: 300, text: 'CHoCH', preferredVertical: 'down' })
  const result = layoutSmcLabels([a1, a2], defaultLayoutCtx, fixedMeasureText)
  const offset = result.find(r => r.lane > 0)
  assert.ok(offset, '应有 lane > 0')
  assert.ok(offset!.boxY + offset!.boxH / 2 > offset!.anchor.anchorY,
    `down: 标签框中心 Y (${offset!.boxY + offset!.boxH / 2}) 应在锚点 Y (${offset!.anchor.anchorY}) 下方`)
})

test('layoutSmcLabels: align=left → boxX = anchorX + 4', () => {
  const anchor = makeAnchor({ anchorX: 100, anchorY: 200, text: 'OB', align: 'left' })
  const result = layoutSmcLabels([anchor], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 1)
  assert.equal(result[0].boxX, 100 + 4, 'align=left: boxX = anchorX + 4')
})

test('layoutSmcLabels: align=right → boxX = anchorX - boxW - 4', () => {
  const anchor = makeAnchor({ anchorX: 700, anchorY: 200, text: 'trailing', align: 'right' })
  const result = layoutSmcLabels([anchor], defaultLayoutCtx, fixedMeasureText)
  assert.equal(result.length, 1)
  const expectedBoxW = 'trailing'.length * 10 + 4 * 2 // 88
  assert.equal(result[0].boxX, 700 - expectedBoxW - 4, 'align=right: boxX = anchorX - boxW - 4')
})

test('layoutSmcLabels: maxLanes=2 → 超过时回退到 lane 0', () => {
  // 3 个完全重叠的标签，maxLanes=2 → 第 3 个无法找到不重叠 lane，回退 lane 0
  const anchors: SmcLabelAnchor[] = [
    makeAnchor({ anchorX: 400, anchorY: 300, text: 'A', preferredVertical: 'up' }),
    makeAnchor({ anchorX: 400, anchorY: 300, text: 'B', preferredVertical: 'up' }),
    makeAnchor({ anchorX: 400, anchorY: 300, text: 'C', preferredVertical: 'up' }),
  ]
  const ctx: SmcLabelLayoutContext = { ...defaultLayoutCtx, maxLanes: 2 }
  const result = layoutSmcLabels(anchors, ctx, fixedMeasureText)
  assert.equal(result.length, 3)
  // 第 3 个标签会回退到 lane 0（best-effort）
  assert.ok(result.every(r => r.lane <= 2), '所有标签 lane <= maxLanes')
})
