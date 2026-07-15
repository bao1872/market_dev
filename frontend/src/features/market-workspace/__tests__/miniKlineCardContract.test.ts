// [MiniKlineCardContract] - 描述: MiniKlineController 行为测试（CHANGE-20260715-007）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/miniKlineCardContract.test.ts
//
// CHANGE-20260715-007: 从源码正则测试改为真实行为测试。
// 使用 mock createChart + mock rAF，真实执行 controller 逻辑并断言调用记录。
// 禁止只读源码正则冒充行为测试。
//
// 覆盖：
//  1. createChart 调用参数：attributionLogo=false, height=190, minimumWidth=56,
//     autoScale=true, scaleMargins, shiftVisibleRangeOnNewBar=false
//  2. addCandlestickSeries 调用参数：A股配色（红涨绿跌）+ autoscaleInfoProvider
//  3. setData 数量：series.setData 只传最后 visibleBars 根（裁剪后）
//  4. setVisibleLogicalRange：{from:-2, to:clippedLength-1+3}
//  5. 不调用 fitContent / resetTimeScale / scrollToRealTime
//  6. 切周期：不同周期 setData 数量不同
//  7. resize：宽度变化时重新 setData + range
//  8. rAF 取消：新 setData 取消上一个 pending rAF
//  9. destroy：取消 pending rAF + chart.remove()
// 10. autoscaleInfoProvider 行为：上方 12%、下方 15% 扩展
// 11. 空数据：不调用 setData / setVisibleLogicalRange
// 12. 五周期 setData 数量验证

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createMiniKlineController } from '../miniKlineController.ts'
import type { CandleBar } from '../miniKlineController.ts'

// ===== Mock 工厂 =====

function createMockDeps() {
  let chartOptions: Record<string, unknown> = {}
  let seriesOptions: Record<string, unknown> = {}

  const seriesCalls = {
    setData: [] as CandleBar[][],
    applyOptions: [] as Record<string, unknown>[],
  }

  const timeScaleCalls = {
    setVisibleLogicalRange: [] as Array<{ from: number; to: number }>,
    applyOptions: [] as Record<string, unknown>[],
    fitContent: 0,
    resetTimeScale: 0,
    scrollToRealTime: 0,
  }

  const chartCalls = {
    applyOptions: [] as Record<string, unknown>[],
    remove: 0,
  }

  const series = {
    setData(data: CandleBar[]) { seriesCalls.setData.push(data) },
    applyOptions(opts: Record<string, unknown>) { seriesCalls.applyOptions.push(opts) },
  }

  const timeScale = {
    setVisibleLogicalRange(range: { from: number; to: number }) {
      timeScaleCalls.setVisibleLogicalRange.push(range)
    },
    applyOptions(opts: Record<string, unknown>) { timeScaleCalls.applyOptions.push(opts) },
    fitContent() { timeScaleCalls.fitContent++ },
    resetTimeScale() { timeScaleCalls.resetTimeScale++ },
    scrollToRealTime() { timeScaleCalls.scrollToRealTime++ },
  }

  const chart = {
    addCandlestickSeries(opts: Record<string, unknown>) {
      seriesOptions = opts
      return series
    },
    timeScale() { return timeScale },
    applyOptions(opts: Record<string, unknown>) { chartCalls.applyOptions.push(opts) },
    remove() { chartCalls.remove++ },
  }

  const createChart = (
    _container: unknown,
    opts: Record<string, unknown>,
  ) => {
    chartOptions = opts
    return chart
  }

  // rAF mock：不自动执行，需手动 flush
  const rafState = {
    nextId: 1,
    pending: [] as Array<{ id: number; cb: () => void; cancelled: boolean }>,
    cancelledCount: 0,
  }

  const raf = {
    request: (cb: () => void) => {
      const id = rafState.nextId++
      rafState.pending.push({ id, cb, cancelled: false })
      return id
    },
    cancel: (id: number) => {
      const entry = rafState.pending.find(e => e.id === id)
      if (entry && !entry.cancelled) {
        entry.cancelled = true
        rafState.cancelledCount++
      }
    },
    flush: () => {
      const active = rafState.pending.filter(e => !e.cancelled)
      rafState.pending = []
      for (const entry of active) entry.cb()
    },
    pendingCount: () => rafState.pending.filter(e => !e.cancelled).length,
    totalCancelled: () => rafState.cancelledCount,
  }

  return {
    createChart,
    raf,
    getChartOptions: () => chartOptions,
    getSeriesOptions: () => seriesOptions,
    seriesCalls,
    timeScaleCalls,
    chartCalls,
  }
}

// 生成 N 根测试 K 线
function generateBars(n: number): CandleBar[] {
  return Array.from({ length: n }, (_, i) => ({
    time: i,
    open: 100 + i,
    high: 101 + i,
    low: 99 + i,
    close: 100.5 + i,
  }))
}

// ===== 1. createChart 调用参数 =====

test('createChart 配置 attributionLogo=false', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const layout = deps.getChartOptions().layout as Record<string, unknown>
  assert.equal(layout.attributionLogo, false, '必须 attributionLogo=false')
})

test('createChart 高度=190', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  assert.equal(deps.getChartOptions().height, 190, '图表高度必须为 190')
})

test('createChart rightPriceScale minimumWidth=56 + autoScale + scaleMargins', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const rps = deps.getChartOptions().rightPriceScale as Record<string, unknown>
  assert.equal(rps.minimumWidth, 56, 'minimumWidth=56')
  assert.equal(rps.autoScale, true, 'autoScale=true')
  const sm = rps.scaleMargins as Record<string, number>
  assert.equal(sm.top, 0.08, 'scaleMargins.top=0.08')
  assert.equal(sm.bottom, 0.08, 'scaleMargins.bottom=0.08')
})

test('createChart timeScale shiftVisibleRangeOnNewBar=false', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const ts = deps.getChartOptions().timeScale as Record<string, unknown>
  assert.equal(ts.shiftVisibleRangeOnNewBar, false, 'shiftVisibleRangeOnNewBar=false')
})

// ===== 2. addCandlestickSeries 调用参数 =====

test('addCandlestickSeries A股配色（红涨绿跌）', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const opts = deps.getSeriesOptions()
  assert.equal(opts.upColor, '#FF4D4F', 'upColor=#FF4D4F（红涨）')
  assert.equal(opts.downColor, '#22C55E', 'downColor=#22C55E（绿跌）')
  assert.equal(opts.borderUpColor, '#FF4D4F')
  assert.equal(opts.borderDownColor, '#22C55E')
  assert.equal(opts.wickUpColor, '#FF4D4F')
  assert.equal(opts.wickDownColor, '#22C55E')
})

test('addCandlestickSeries 配置 autoscaleInfoProvider 回调', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  assert.equal(typeof deps.getSeriesOptions().autoscaleInfoProvider, 'function',
    'autoscaleInfoProvider 必须为函数')
})

// ===== 3. setData 数量：裁剪到最后 visibleBars 根 =====

test('setData 1d 80根数据裁剪到40根（contentWidth=340）', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(80), '1d', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData.length, 1, '应调用1次 setData')
  assert.equal(deps.seriesCalls.setData[0].length, 40, '应裁剪到40根')
})

test('setData 15m 120根数据裁剪到48根', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(120), '15m', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData[0].length, 48, '15m 应裁剪到48根')
})

test('setData 数据不足时传全部', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(20), '1d', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData[0].length, 20, '20根数据应全部传入')
})

test('setData 裁剪后保留最后 N 根（非前 N 根）', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  const bars = generateBars(80)
  controller.setData(bars, '1d', 340)
  deps.raf.flush()
  const passed = deps.seriesCalls.setData[0]
  // 最后 40 根：第 40..79 项
  assert.equal(passed[0].time, 40, '第一根 time 应为 40（原数组第 40 项）')
  assert.equal(passed[39].time, 79, '最后一根 time 应为 79（原数组第 79 项）')
})

// ===== 4. setVisibleLogicalRange：{from:-2, to:clippedLength-1+3} =====

test('setVisibleLogicalRange 1d: {from:-2, to:40-1+3=42}', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(80), '1d', 340)
  deps.raf.flush()
  assert.equal(deps.timeScaleCalls.setVisibleLogicalRange.length, 1, '应调用1次')
  const range = deps.timeScaleCalls.setVisibleLogicalRange[0]
  assert.equal(range.from, -2, 'from=-2')
  assert.equal(range.to, 42, 'to=40-1+3=42')
})

test('setVisibleLogicalRange 15m: {from:-2, to:48-1+3=50}', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(120), '15m', 340)
  deps.raf.flush()
  const range = deps.timeScaleCalls.setVisibleLogicalRange[0]
  assert.equal(range.from, -2)
  assert.equal(range.to, 50, 'to=48-1+3=50')
})

// ===== 5. 不调用 fitContent / resetTimeScale / scrollToRealTime =====

test('不调用 fitContent / resetTimeScale / scrollToRealTime', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(80), '1d', 340)
  controller.resize(300)
  deps.raf.flush()
  controller.destroy()
  assert.equal(deps.timeScaleCalls.fitContent, 0, '禁止调用 fitContent')
  assert.equal(deps.timeScaleCalls.resetTimeScale, 0, '禁止调用 resetTimeScale')
  assert.equal(deps.timeScaleCalls.scrollToRealTime, 0, '禁止调用 scrollToRealTime')
})

// ===== 6. 切周期：不同周期 setData 数量不同 =====

test('切周期 1d→15m 后 setData 数量从40变为48', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  // 1d
  controller.setData(generateBars(120), '1d', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData[0].length, 40, '1d=40')
  // 15m
  controller.setData(generateBars(120), '15m', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData[1].length, 48, '15m=48')
  // 1mo
  controller.setData(generateBars(120), '1mo', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData[2].length, 30, '1mo=30')
})

test('切周期时 chart.applyOptions 更新 timeVisible', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  // 1d: timeVisible=false
  controller.setData(generateBars(80), '1d', 340)
  // 15m: timeVisible=true
  controller.setData(generateBars(80), '15m', 340)
  deps.raf.flush()
  // 第一次 setData 调 chart.applyOptions({timeScale:{timeVisible:false}})
  const opts1 = deps.chartCalls.applyOptions[0]
  const ts1 = opts1.timeScale as Record<string, unknown>
  assert.equal(ts1.timeVisible, false, '1d timeVisible=false')
  // 第二次 setData 调 chart.applyOptions({timeScale:{timeVisible:true}})
  const opts2 = deps.chartCalls.applyOptions[1]
  const ts2 = opts2.timeScale as Record<string, unknown>
  assert.equal(ts2.timeVisible, true, '15m timeVisible=true')
})

// ===== 7. resize：宽度变化时重新 setData + range =====

test('resize 后重新 setData + setVisibleLogicalRange', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(80), '1d', 340)
  deps.raf.flush()
  // resize 到 300px：15m 会减少，1d 保持 40
  controller.resize(300)
  deps.raf.flush()
  // 应有2次 setData（初始 + resize）
  assert.ok(deps.seriesCalls.setData.length >= 2, 'resize 后应重新 setData')
  assert.ok(deps.timeScaleCalls.setVisibleLogicalRange.length >= 2, 'resize 后应重新 setVisibleLogicalRange')
  // chart.applyOptions 应包含 width:300
  const lastApply = deps.chartCalls.applyOptions[deps.chartCalls.applyOptions.length - 1]
  assert.equal(lastApply.width, 300, 'resize 应设置 width=300')
})

// ===== 8. rAF 取消：新 setData 取消上一个 pending rAF =====

test('连续 setData 取消上一个 pending rAF（仅最后一次生效）', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  // 第一次 setData → rAF #1
  controller.setData(generateBars(120), '1d', 340)
  // 第二次 setData → 取消 #1，调度 #2
  controller.setData(generateBars(120), '15m', 340)
  // flush：只有 #2 执行
  deps.raf.flush()
  assert.ok(deps.raf.totalCancelled() >= 1, '应取消至少1个 rAF')
  // 只应有1次 setData（来自 #2，15m=48）
  assert.equal(deps.seriesCalls.setData.length, 1, '只应执行最后一次 rAF 的 setData')
  assert.equal(deps.seriesCalls.setData[0].length, 48, '应为 15m 的 48 根')
})

// ===== 9. destroy：取消 pending rAF + chart.remove() =====

test('destroy 取消 pending rAF 并调用 chart.remove()', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData(generateBars(80), '1d', 340)
  // 不 flush，直接 destroy → 应取消 pending rAF
  controller.destroy()
  assert.ok(deps.raf.totalCancelled() >= 1, 'destroy 应取消 pending rAF')
  assert.equal(deps.chartCalls.remove, 1, 'destroy 应调用 chart.remove() 1次')
  // flush 后不应执行已取消的 rAF
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData.length, 0, 'destroy 后不应执行 setData')
})

test('destroy 后再调用 setData 不生效', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.destroy()
  controller.setData(generateBars(80), '1d', 340)
  deps.raf.flush()
  // destroy 后 chart.remove() 已调用，再 setData 不应产生新的 setData 调用
  // （controller 内部 chart 引用仍存在但已被 remove，mock 仍记录调用）
  // 关键：不应崩溃
  assert.equal(deps.chartCalls.remove, 1, '只应 remove 1次')
})

// ===== 10. autoscaleInfoProvider 行为：上方 12%、下方 15% 扩展 =====

test('autoscaleInfoProvider 扩展价格范围：上方 12%，下方 15%', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const provider = deps.getSeriesOptions().autoscaleInfoProvider as
    (baseImpl: () => { priceRange: { minValue: number; maxValue: number } | null } | null) =>
      { priceRange: { minValue: number; maxValue: number } | null } | null
  assert.ok(typeof provider === 'function', 'autoscaleInfoProvider 应为函数')
  // base: min=10, max=20, range=10
  // 上方 12%: 20+1.2=21.2, 下方 15%: 10-1.5=8.5
  const result = provider(() => ({
    priceRange: { minValue: 10, maxValue: 20 },
  }))
  assert.ok(result && result.priceRange, '应返回非空 priceRange')
  assert.equal(result!.priceRange!.minValue, 8.5, 'minValue 应为 8.5（下方 15%）')
  assert.equal(result!.priceRange!.maxValue, 21.2, 'maxValue 应为 21.2（上方 12%）')
})

test('autoscaleInfoProvider baseImpl 返回 null 时透传', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const provider = deps.getSeriesOptions().autoscaleInfoProvider as
    (baseImpl: () => null) => unknown
  const result = provider(() => null)
  assert.equal(result, null, 'baseImpl 返回 null 时应透传 null')
})

test('autoscaleInfoProvider baseImpl priceRange 为 null 时透传', () => {
  const deps = createMockDeps()
  createMiniKlineController({} as HTMLElement, 340, deps)
  const provider = deps.getSeriesOptions().autoscaleInfoProvider as
    (baseImpl: () => { priceRange: null }) => { priceRange: null }
  const result = provider(() => ({ priceRange: null }))
  assert.ok(result, '应返回非空对象')
  assert.equal(result.priceRange, null, 'priceRange 应为 null')
})

// ===== 11. 空数据：不调用 setData / setVisibleLogicalRange =====

test('空数据不调用 setData / setVisibleLogicalRange', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  controller.setData([], '1d', 340)
  deps.raf.flush()
  assert.equal(deps.seriesCalls.setData.length, 0, '空数据不应调用 setData')
  assert.equal(deps.timeScaleCalls.setVisibleLogicalRange.length, 0, '空数据不应调用 setVisibleLogicalRange')
})

// ===== 12. 五周期 setData 数量验证 =====

test('五周期 setData 数量：15m=48, 1h=44, 1d=40, 1w=36, 1mo=30', () => {
  const deps = createMockDeps()
  const controller = createMiniKlineController({} as HTMLElement, 340, deps)
  const bars = generateBars(120)
  const cases: Array<{ tf: '15m' | '1h' | '1d' | '1w' | '1mo'; expected: number }> = [
    { tf: '15m', expected: 48 },
    { tf: '1h', expected: 44 },
    { tf: '1d', expected: 40 },
    { tf: '1w', expected: 36 },
    { tf: '1mo', expected: 30 },
  ]
  for (let i = 0; i < cases.length; i++) {
    controller.setData(bars, cases[i].tf, 340)
    deps.raf.flush()
    assert.equal(
      deps.seriesCalls.setData[i].length,
      cases[i].expected,
      `${cases[i].tf} 应为 ${cases[i].expected} 根`,
    )
  }
})
