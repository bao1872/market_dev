// [趋势选股] - 描述: adapter 全量 universe 改造契约测试
// 用法：node --experimental-strip-types --test src/features/trend-selection/__tests__/adapter.test.ts
//
// 覆盖：
// 1. succeeded 行（有 id/payload）正常转换
// 2. skipped 行（id=null, payload=null, reason_code='insufficient_history'）→ resultId='', payload={}
// 3. failed 行（id=null, payload=null, error_message='...'）→ resultId='', payload={}
// 4. watched 字段正确传递（基于 instrumentId 匹配）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import { adaptStrategyResultToTrendRow } from '../adapters.ts'
import { adaptMarketStockToTrendRow } from '../adapters.ts'
import type { StrategyResult, MarketStockRow } from '@/api/endpoints'

// ===== 辅助：构造测试行 =====
function makeSucceededRow(): StrategyResult {
  return {
    id: 'result-001',
    run_id: 'run-001',
    strategy_version_id: 'ver-001',
    instrument_id: 'inst-001',
    instrument_symbol: '000001',
    instrument_name: '平安银行',
    instrument_market: 'SZ',
    trade_date: '2026-07-04',
    payload: { dsa_dir_bars: 40, offset_mean: 0.01 },
    created_at: '2026-07-04T10:00:00Z',
    item_status: 'succeeded',
  } as StrategyResult
}

function makeSkippedRow(): StrategyResult {
  return {
    id: null,
    run_id: null,
    strategy_version_id: null,
    instrument_id: 'inst-002',
    instrument_symbol: '000002',
    instrument_name: '万科A',
    instrument_market: 'SZ',
    trade_date: null,
    payload: null,
    created_at: null,
    item_status: 'skipped',
    reason_code: 'insufficient_history',
  } as StrategyResult
}

function makeFailedRow(): StrategyResult {
  return {
    id: null,
    run_id: null,
    strategy_version_id: null,
    instrument_id: 'inst-003',
    instrument_symbol: '000003',
    instrument_name: '中集集团',
    instrument_market: 'SZ',
    trade_date: null,
    payload: null,
    created_at: null,
    item_status: 'failed',
    error_message: 'bars data unavailable',
  } as StrategyResult
}

// ===== 1. succeeded 行正常转换 =====
test('adaptStrategyResultToTrendRow: succeeded 行保留 id 和 payload', () => {
  const row = adaptStrategyResultToTrendRow(makeSucceededRow())
  assert.equal(row.resultId, 'result-001')
  assert.equal(row.instrumentId, 'inst-001')
  assert.equal(row.symbol, '000001')
  assert.equal(row.name, '平安银行')
  assert.equal(row.market, 'SZ')
  assert.deepEqual(row.payload, { dsa_dir_bars: 40, offset_mean: 0.01 })
  assert.equal(row.watched, false)
})

// ===== 2. skipped 行 resultId='', payload={} =====
test('adaptStrategyResultToTrendRow: skipped 行 resultId 为空字符串, payload 为空对象', () => {
  const row = adaptStrategyResultToTrendRow(makeSkippedRow())
  assert.equal(row.resultId, '', `skipped 行 resultId 应为空字符串，实际=${row.resultId}`)
  assert.equal(row.instrumentId, 'inst-002')
  assert.equal(row.symbol, '000002')
  assert.equal(row.name, '万科A')
  assert.deepEqual(row.payload, {}, `skipped 行 payload 应为空对象，实际=${JSON.stringify(row.payload)}`)
  assert.equal(row.watched, false)
})

// ===== 3. failed 行 resultId='', payload={} =====
test('adaptStrategyResultToTrendRow: failed 行 resultId 为空字符串, payload 为空对象', () => {
  const row = adaptStrategyResultToTrendRow(makeFailedRow())
  assert.equal(row.resultId, '', `failed 行 resultId 应为空字符串，实际=${row.resultId}`)
  assert.equal(row.instrumentId, 'inst-003')
  assert.equal(row.symbol, '000003')
  assert.equal(row.name, '中集集团')
  assert.deepEqual(row.payload, {}, `failed 行 payload 应为空对象，实际=${JSON.stringify(row.payload)}`)
})

// ===== 4. watched 字段基于 instrumentId 匹配 =====
test('adaptStrategyResultToTrendRow: watched 基于 instrumentId 匹配', () => {
  const watchedIds = new Set<string>(['inst-001', 'inst-003'])
  const succeededRow = adaptStrategyResultToTrendRow(makeSucceededRow(), watchedIds)
  const skippedRow = adaptStrategyResultToTrendRow(makeSkippedRow(), watchedIds)
  const failedRow = adaptStrategyResultToTrendRow(makeFailedRow(), watchedIds)
  assert.equal(succeededRow.watched, true, 'inst-001 应在 watchedIds 中')
  assert.equal(skippedRow.watched, false, 'inst-002 不应在 watchedIds 中')
  assert.equal(failedRow.watched, true, 'inst-003 应在 watchedIds 中')
})

// ===== [CHANGE-20260729-009] adaptMarketStockToTrendRow 测试 =====

function makeMarketStockRow(overrides: Partial<MarketStockRow> = {}): MarketStockRow {
  return {
    instrument_id: 'inst-001',
    symbol: '000001',
    name: '平安银行',
    latest_price: 12.5,
    change_pct: 2.3,
    industry: '银行',
    concepts: ['金融科技'],
    dsa_state: '上行',
    structure_state: '成本区间上方',
    latest_event_title: null,
    latest_event_time: null,
    is_watchlisted: true,
    first_pyramid: { fp_trend_direction: 'up', fp_sqzmom_value: 0.5 },
    payload: { dsa_dir_bars: 40, offset_mean: 0.01 },
    data_run_id: 'run-abc',
    factor_ready: true,
    factor_error: null,
    factor_actual_bars: null,
    factor_required_bars: null,
    chip_status: {
      state: 'ready',
      reasonCode: null,
      reasonText: '已计算',
      computedAt: '2026-07-29T15:00:00+08:00',
      actualBars: null,
      requiredBars: null,
      fullQualityBars: null,
    },
    ...overrides,
  }
}

// ===== 5. 正常 MarketStockRow 转换 =====
test('adaptMarketStockToTrendRow: 正常行保留全部字段', () => {
  const row = adaptMarketStockToTrendRow(makeMarketStockRow())
  assert.equal(row.instrumentId, 'inst-001')
  assert.equal(row.symbol, '000001')
  assert.equal(row.name, '平安银行')
  assert.equal(row.watched, true, 'is_watchlisted 应映射为 watched')
  assert.deepEqual(row.payload, { dsa_dir_bars: 40, offset_mean: 0.01 })
  assert.deepEqual(row.firstPyramid, { fp_trend_direction: 'up', fp_sqzmom_value: 0.5 })
  assert.equal(row.dataRunId, 'run-abc')
  assert.equal(row.factorReady, true)
  assert.equal(row.factorError, null)
  assert.equal(row.latestChangePct, 2.3)
  assert.equal(row.dsaState, '上行')
  assert.equal(row.industry, '银行')
  assert.deepEqual(row.concepts, ['金融科技'])
})

// ===== 6. null first_pyramid + INSUFFICIENT_DAILY_BARS =====
test('adaptMarketStockToTrendRow: 新股数据不足场景', () => {
  const row = adaptMarketStockToTrendRow(makeMarketStockRow({
    first_pyramid: null,
    factor_ready: false,
    factor_error: 'INSUFFICIENT_DAILY_BARS',
    factor_actual_bars: 45,
    factor_required_bars: 60,
    is_watchlisted: false,
    payload: null,
  }))
  assert.equal(row.firstPyramid, null)
  assert.equal(row.factorReady, false)
  assert.equal(row.factorError, 'INSUFFICIENT_DAILY_BARS')
  assert.equal(row.factorActualBars, 45)
  assert.equal(row.factorRequiredBars, 60)
  assert.equal(row.watched, false)
  assert.deepEqual(row.payload, {}, 'null payload 应为空对象')
})

// ===== 7. chip_status unavailable + M15_BARS_INSUFFICIENT =====
test('adaptMarketStockToTrendRow: chip 状态为 M15_BARS_INSUFFICIENT', () => {
  const row = adaptMarketStockToTrendRow(makeMarketStockRow({
    chip_status: {
      state: 'unavailable',
      reasonCode: 'M15_BARS_INSUFFICIENT',
      reasonText: '15分钟数据不足（354根，需≥500；4000根为完整质量门槛）',
      computedAt: null,
      actualBars: 354,
      requiredBars: 500,
      fullQualityBars: 4000,
    },
  }))
  // chipStatus 是 unknown 自定义字段，需断言为 MarketStockRow['chip_status'] 类型
  const chipStatus = row.chipStatus as MarketStockRow['chip_status']
  assert.ok(chipStatus, 'chipStatus 应非空')
  assert.equal(chipStatus!.state, 'unavailable')
  assert.equal(chipStatus!.reasonCode, 'M15_BARS_INSUFFICIENT')
  assert.equal(chipStatus!.actualBars, 354)
  assert.equal(chipStatus!.requiredBars, 500)
  assert.equal(chipStatus!.fullQualityBars, 4000)
  assert.equal(chipStatus!.reasonText, '15分钟数据不足（354根，需≥500；4000根为完整质量门槛）')
})

// ===== 8. resultId 使用 instrument_id（MarketStockRow 无 resultId） =====
test('adaptMarketStockToTrendRow: resultId = instrument_id', () => {
  const row = adaptMarketStockToTrendRow(makeMarketStockRow())
  assert.equal(row.resultId, 'inst-001', 'resultId 应等于 instrument_id')
  assert.equal(row.market, '', 'MarketStockRow 不含 market，应为空字符串')
})
