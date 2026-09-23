// [ChartAvailabilityIsolation] - PANJI-TDX-RELIABILITY-PARITY-03 前端契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/chartAvailabilityIsolationContract.test.ts
//
// 覆盖 Task 3 前端闭环：
//   1. ChartSnapshotResponse 类型含 degraded / degraded_reason（后端真实字段）
//   2. chart-snapshot 本地把后端 typed 503 转成友好文案，绝不显示
//      "Request failed with status code 503"
//   3. optional 1d/1w/1mo outage → provider-unavailable 状态，
//      不是 display_frame mismatch（两种语义严格区分）
//   4. 真实 frame mismatch 仍然显示原 mismatch 错误（不得削弱一致性保护）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  LiveMarketDataUnavailableError,
  isLiveMarketDataUnavailableDetail,
} from '../../../api/liveMarketDataUnavailable.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const _srcRoot = join(__dirname, '../../..')

function readSrc(rel: string): string {
  return readFileSync(join(_srcRoot, rel), 'utf8')
}

// ===== 1. 后端响应类型合同 =====

test('FIX1: ChartSnapshotResponse 声明 degraded: boolean 与 degraded_reason', () => {
  const src = readSrc('api/stockData.ts')
  const iface = src.slice(
    src.indexOf('export interface ChartSnapshotResponse'),
    src.indexOf('export class LiveMarketDataUnavailableError'),
  )
  assert.ok(
    /degraded:\s*boolean/.test(iface),
    'ChartSnapshotResponse 必须声明 degraded: boolean（后端已返回该字段）',
  )
  assert.ok(
    /degraded_reason:\s*string\s*\|\s*null/.test(iface),
    'ChartSnapshotResponse 必须声明 degraded_reason: string | null',
  )
})

// ===== 2. typed 503 → 友好文案 =====

test('FIX2: 503 + LIVE_MARKET_DATA_UNAVAILABLE 被识别', () => {
  assert.equal(
    isLiveMarketDataUnavailableDetail({
      code: 'LIVE_MARKET_DATA_UNAVAILABLE',
      timeframe: '15m',
      provider_family: 'tdx',
      message: '实时分钟行情暂不可用，请稍后重试',
    }),
    true,
  )
  // 非本错误的 503 / 其它 code 不得误判
  assert.equal(isLiveMarketDataUnavailableDetail({ code: 'OTHER' }), false)
  assert.equal(isLiveMarketDataUnavailableDetail(null), false)
  assert.equal(isLiveMarketDataUnavailableDetail(undefined), false)
})

test('FIX2: typed error 携带机器字段与用户可见文案', () => {
  const err = new LiveMarketDataUnavailableError(
    '实时分钟行情暂不可用，请稍后重试', '15m', 'tdx',
  )
  assert.equal(err.code, 'LIVE_MARKET_DATA_UNAVAILABLE')
  assert.equal(err.status, 503)
  assert.equal(err.timeframe, '15m')
  assert.equal(err.providerFamily, 'tdx')
  assert.equal(err.message, '实时分钟行情暂不可用，请稍后重试')
  assert.ok(err instanceof Error)
})

test('FIX2: getChartSnapshot 本地捕获 503 并抛出 typed error（不依赖 Axios message）', () => {
  const src = readSrc('api/stockData.ts')
  const fn = src.slice(
    src.indexOf('export async function getChartSnapshot'),
    src.indexOf('// ============================================================\n// ===== Calendar'),
  )
  assert.ok(
    /response\?\.status\s*===\s*503/.test(fn),
    'getChartSnapshot 必须识别 HTTP 503',
  )
  assert.ok(
    /isLiveMarketDataUnavailableDetail/.test(fn),
    'getChartSnapshot 必须用 detail.code 判别，而非依赖 Axios error.message',
  )
  assert.ok(
    /实时分钟行情暂不可用，请稍后重试/.test(fn),
    '必须提供后端友好文案兜底',
  )
})

test('FIX2: UI 不得展示 "Request failed with status code 503"', () => {
  const ws = readSrc('features/stock-research/StockResearchWorkspace.tsx')
  // 命中 typed error 时优先展示其 message，不走通用 error.message 分支
  assert.ok(
    /_pickLiveUnavailableError/.test(ws),
    '必须先判别 typed 503，再走通用错误分支',
  )
  assert.ok(
    /liveUnavailableError\.message/.test(ws),
    'typed 503 场景必须展示后端友好文案',
  )
  const typedBranch = ws.slice(
    ws.indexOf('const liveUnavailableError = _pickLiveUnavailableError'),
    ws.indexOf('if (barsQuery.isError)'),
  )
  assert.ok(
    !/Request failed with status code/.test(typedBranch),
    'typed 分支不得出现 Axios 原生文案',
  )
})

// ===== 3. optional outage ≠ frame mismatch =====

test('FIX3: StrategyChart 具备显式 unavailable 状态（不是 frameMismatch）', () => {
  const src = readSrc('components/StrategyChart.tsx')
  assert.ok(
    /indicatorsUnavailable\?: boolean/.test(src),
    'StrategyChart props 必须新增 indicatorsUnavailable',
  )
  assert.ok(
    /'pending'\s*\|\s*'success'\s*\|\s*'mismatch-error'\s*\|\s*'unavailable'/.test(src),
    '状态机必须扩展为 4 态（含 unavailable）',
  )
  assert.ok(
    /chart-indicators-unavailable-banner/.test(src),
    '必须有独立的 provider-unavailable 提示节点',
  )
  // unavailable 优先于 frame 判定
  const state = src.slice(
    src.indexOf("const indicatorsLoadState:"),
    src.indexOf('return (\n    <div className="strategy-chart-wrap">'),
  )
  const unavailableIdx = state.indexOf("indicatorsUnavailable\n")
  const mismatchIdx = state.indexOf('frameMismatch')
  assert.ok(unavailableIdx > 0, 'unavailable 分支必须存在')
  assert.ok(
    unavailableIdx < mismatchIdx,
    'unavailable 判定必须优先于 frameMismatch（否则 outage 被误判为不一致）',
  )
})

test('FIX3: 真实 frame mismatch 仍保留原错误横幅（未被削弱）', () => {
  const src = readSrc('components/StrategyChart.tsx')
  assert.ok(
    /指标加载失败：display_frame 不匹配/.test(src),
    '真实 display_frame 不一致必须继续显示原错误',
  )
  assert.ok(
    /chart-frame-mismatch-banner/.test(src),
    'mismatch 横幅 testid 必须保留',
  )
})

test('FIX3: workspace 把 chartDegraded 传给 StrategyChart.indicatorsUnavailable', () => {
  const ws = readSrc('features/stock-research/StockResearchWorkspace.tsx')
  assert.ok(
    /indicatorsUnavailable=\{chartDegraded\}/.test(ws),
    '可选富化降级必须驱动 StrategyChart 的 unavailable 状态',
  )
})

test('FIX3: 降级提示文案为 provider unavailable 语义', () => {
  const src = readSrc('components/StrategyChart.tsx')
  assert.ok(
    /指标暂不可用（实时分钟行情中断）/.test(src),
    'unavailable 提示必须明确是实时分钟行情中断',
  )
})
