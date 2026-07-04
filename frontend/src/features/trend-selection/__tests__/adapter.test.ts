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
import type { StrategyResult } from '@/api/endpoints'

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
