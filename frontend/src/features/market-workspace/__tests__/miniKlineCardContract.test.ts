// [MiniKlineCardContract] - 描述: MiniKlineCard 源码契约测试（CHANGE-20260715-002）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/miniKlineCardContract.test.ts
//
// 覆盖：
// 1. 不调用 fitContent / resetTimeScale / scrollToRealTime（禁止覆盖自定义 range）
// 2. 调用 setVisibleLogicalRange（setData 后在 rAF 中执行）
// 3. 使用 computeMiniKlineViewport 纯函数计算 range
// 4. 使用 computeAutoscaleRange 扩展价格范围（autoscaleInfoProvider）
// 5. ResizeObserver 响应式 + 宽度变化时重新应用 range
// 6. requestAnimationFrame 延迟应用 range（避免 setData 竞态）
// 7. 五周期按钮（15m/60m/日/周/月）
// 8. attributionLogo=false（移除 TV 标志）
// 9. 图表高度固定 190px
// 10. minimumWidth=MIN_PRICE_SCALE_WIDTH（56px）
// 11. autoScale=true + scaleMargins {top:0.08, bottom:0.08}
// 12. shiftVisibleRangeOnNewBar=false（新数据不漂移）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const CARD_PATH = join(__dirname, '..', 'MiniKlineCard.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 不调用 fitContent / resetTimeScale / scrollToRealTime =====
test('MiniKlineCard 不调用 fitContent / resetTimeScale / scrollToRealTime', () => {
  const src = readSource(CARD_PATH)
  // 检查是否调用（带括号），允许在注释中出现
  assert.ok(!/\.fitContent\(/.test(src), '禁止调用 fitContent（会覆盖自定义 range）')
  assert.ok(!/\.resetTimeScale\(/.test(src), '禁止调用 resetTimeScale')
  assert.ok(!/\.scrollToRealTime\(/.test(src), '禁止调用 scrollToRealTime')
})

// ===== 2. 调用 setVisibleLogicalRange =====
test('MiniKlineCard 调用 setVisibleLogicalRange', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('setVisibleLogicalRange'),
    'setData 后必须调用 setVisibleLogicalRange 应用自定义 range',
  )
})

// ===== 3. 使用 computeMiniKlineViewport 纯函数 =====
test('MiniKlineCard 使用 computeMiniKlineViewport 计算范围', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('computeMiniKlineViewport'),
    '必须使用 computeMiniKlineViewport 纯函数计算范围',
  )
})

// ===== 4. 使用 computeAutoscaleRange 扩展价格范围 =====
test('MiniKlineCard 使用 computeAutoscaleRange + autoscaleInfoProvider', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('computeAutoscaleRange'),
    '必须使用 computeAutoscaleRange 扩展价格范围',
  )
  assert.ok(
    src.includes('autoscaleInfoProvider'),
    '必须配置 autoscaleInfoProvider 回调',
  )
})

// ===== 5. ResizeObserver 响应式 =====
test('MiniKlineCard 使用 ResizeObserver 响应式调整宽度', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes('ResizeObserver'), '必须使用 ResizeObserver 监听容器宽度变化')
  assert.ok(src.includes('disconnect'), '卸载时必须调用 disconnect()')
})

// ===== 6. requestAnimationFrame 延迟应用 range =====
test('MiniKlineCard 使用 requestAnimationFrame 延迟应用 range', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('requestAnimationFrame'),
    'setData 后必须在 rAF 中执行 setVisibleLogicalRange（避免竞态）',
  )
})

// ===== 7. 五周期按钮 =====
test('MiniKlineCard 包含五周期按钮（15m/60m/日/周/月）', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes("'15m'"), '必须包含 15m 周期')
  assert.ok(src.includes("'1h'"), '必须包含 1h（60m）周期')
  assert.ok(src.includes("'1d'"), '必须包含 1d（日）周期')
  assert.ok(src.includes("'1w'"), '必须包含 1w（周）周期')
  assert.ok(src.includes("'1mo'"), '必须包含 1mo（月）周期')
})

// ===== 8. attributionLogo=false =====
test('MiniKlineCard 设置 attributionLogo=false 移除 TV 标志', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('attributionLogo: false'),
    '必须设置 attributionLogo=false 移除 TradingView 标志',
  )
})

// ===== 9. 图表高度固定 190px =====
test('MiniKlineCard 图表高度固定 190px', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes('190'), '图表高度必须为 190px')
  assert.ok(src.includes('CHART_HEIGHT'), '必须使用 CHART_HEIGHT 常量')
})

// ===== 10. minimumWidth=MIN_PRICE_SCALE_WIDTH =====
test('MiniKlineCard 设置 minimumWidth=MIN_PRICE_SCALE_WIDTH', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('MIN_PRICE_SCALE_WIDTH'),
    '必须导入并使用 MIN_PRICE_SCALE_WIDTH',
  )
  assert.ok(
    src.includes('minimumWidth'),
    'rightPriceScale 必须设置 minimumWidth',
  )
})

// ===== 11. autoScale + scaleMargins =====
test('MiniKlineCard 配置 autoScale + scaleMargins', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes('autoScale: true'), 'rightPriceScale 必须 autoScale=true')
  assert.ok(src.includes('scaleMargins'), 'rightPriceScale 必须配置 scaleMargins')
  assert.ok(src.includes('0.08'), 'scaleMargins top/bottom 应为 0.08')
})

// ===== 12. shiftVisibleRangeOnNewBar=false =====
test('MiniKlineCard 设置 shiftVisibleRangeOnNewBar=false', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('shiftVisibleRangeOnNewBar: false'),
    'timeScale 必须 shiftVisibleRangeOnNewBar=false（新数据不漂移）',
  )
})

// ===== 13. chart.remove() 卸载清理 =====
test('MiniKlineCard 卸载时调用 chart.remove() 清理', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes('chart.remove()'), '卸载时必须调用 chart.remove()')
  assert.ok(src.includes('chartRef.current = null'), '卸载后必须清空 chartRef')
  assert.ok(src.includes('seriesRef.current = null'), '卸载后必须清空 seriesRef')
})

// ===== 14. A 股配色：红涨绿跌 =====
test('MiniKlineCard 使用 A 股配色（红涨绿跌）', () => {
  const src = readSource(CARD_PATH)
  assert.ok(src.includes("upColor: '#FF4D4F'"), 'upColor 必须为 #FF4D4F（红涨）')
  assert.ok(src.includes("downColor: '#22C55E'"), 'downColor 必须为 #22C55E（绿跌）')
  assert.ok(src.includes("borderUpColor: '#FF4D4F'"), 'borderUpColor 必须为 #FF4D4F')
  assert.ok(src.includes("borderDownColor: '#22C55E'"), 'borderDownColor 必须为 #22C55E')
  assert.ok(src.includes("wickUpColor: '#FF4D4F'"), 'wickUpColor 必须为 #FF4D4F')
  assert.ok(src.includes("wickDownColor: '#22C55E'"), 'wickDownColor 必须为 #22C55E')
})

// ===== 15. 容器宽度整数化（避免亚像素抖动）=====
test('MiniKlineCard 容器宽度整数化', () => {
  const src = readSource(CARD_PATH)
  assert.ok(
    src.includes('Math.floor(entry.contentRect.width)'),
    'ResizeObserver 回调中必须 Math.floor 整数化宽度',
  )
  assert.ok(
    src.includes('Math.floor(containerRef.current.clientWidth'),
    '初始化时必须 Math.floor 整数化宽度',
  )
})
