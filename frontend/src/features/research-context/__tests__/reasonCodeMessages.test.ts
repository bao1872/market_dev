// [reasonCodeMessages] - 描述: 状态观察面板 reasonCode 文案映射契约测试（测试 9）
// 用法：node --experimental-strip-types --test src/features/research-context/__tests__/reasonCodeMessages.test.ts
//
// 覆盖：
//  1. no_published_full_run → 明确文案，不含"暂无可用状态数据"
//  2. snapshot_missing → 明确文案 + runTradeDate meta
//  3. snapshot_run_not_linked → 明确文案 + "待修复归属" + runTradeDate meta
//  4. legacy_snapshot_ambiguous → 明确文案 + runTradeDate meta
//  5. null → "暂无可用状态数据"
//  6. 未知 reasonCode → null
//  7. 所有已知 reasonCode 都返回非 null（禁止统一显示"暂无可用状态数据"）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { getReasonCodeMessage } from '../reasonCodeMessages.ts'

const FALLBACK = '暂无可用状态数据'

// ===== 1. no_published_full_run =====
test('reasonCode=no_published_full_run 返回明确文案', () => {
  const msg = getReasonCodeMessage('no_published_full_run', null)
  assert.ok(msg, '应返回非 null')
  assert.ok(msg!.title, 'title 应非空')
  assert.notEqual(msg!.title, FALLBACK, '禁止统一显示"暂无可用状态数据"')
  assert.ok(msg!.title.includes('盘后快照'), '应包含"盘后快照"关键词')
  assert.equal(msg!.meta, undefined, 'no_published_full_run 无 meta')
})

// ===== 2. snapshot_missing =====
test('reasonCode=snapshot_missing 返回明确文案 + runTradeDate', () => {
  const msg = getReasonCodeMessage('snapshot_missing', '2026-07-10')
  assert.ok(msg)
  assert.notEqual(msg!.title, FALLBACK)
  assert.ok(msg!.title.includes('快照'), '应包含"快照"关键词')
  assert.ok(msg!.meta!.includes('2026-07-10'), 'meta 应包含 runTradeDate')
})

test('reasonCode=snapshot_missing 无 runTradeDate 时 meta=undefined', () => {
  const msg = getReasonCodeMessage('snapshot_missing', null)
  assert.ok(msg)
  assert.equal(msg!.meta, undefined)
})

// ===== 3. snapshot_run_not_linked =====
test('reasonCode=snapshot_run_not_linked 返回明确文案 + 待修复归属', () => {
  const msg = getReasonCodeMessage('snapshot_run_not_linked', '2026-07-10')
  assert.ok(msg)
  assert.notEqual(msg!.title, FALLBACK)
  assert.ok(msg!.title.includes('关联'), '应包含"关联"关键词')
  assert.ok(msg!.meta!.includes('待修复归属'), 'meta 应包含"待修复归属"')
  assert.ok(msg!.meta!.includes('2026-07-10'), 'meta 应包含 runTradeDate')
})

// ===== 4. legacy_snapshot_ambiguous =====
test('reasonCode=legacy_snapshot_ambiguous 返回明确文案', () => {
  const msg = getReasonCodeMessage('legacy_snapshot_ambiguous', '2026-07-10')
  assert.ok(msg)
  assert.notEqual(msg!.title, FALLBACK)
  assert.ok(msg!.title.includes('归属'), '应包含"归属"关键词')
  assert.ok(msg!.meta!.includes('2026-07-10'), 'meta 应包含 runTradeDate')
})

// ===== 5. null → 暂无可用状态数据 =====
test('reasonCode=null 返回默认文案', () => {
  const msg = getReasonCodeMessage(null, null)
  assert.ok(msg)
  assert.equal(msg!.title, FALLBACK)
  assert.equal(msg!.meta, undefined)
})

// ===== 6. 未知 reasonCode → null =====
test('未知 reasonCode 返回 null', () => {
  const msg = getReasonCodeMessage('unknown_code', null)
  assert.equal(msg, null)
})

// ===== 7. 所有已知 reasonCode 都返回非 null =====
test('所有已知 reasonCode 都返回非 null 且不统一显示默认文案', () => {
  const knownCodes = [
    'no_published_full_run',
    'snapshot_missing',
    'snapshot_run_not_linked',
    'legacy_snapshot_ambiguous',
  ]
  for (const code of knownCodes) {
    const msg = getReasonCodeMessage(code, '2026-07-10')
    assert.ok(msg, `${code} 应返回非 null`)
    assert.notEqual(msg!.title, FALLBACK, `${code} 禁止统一显示"${FALLBACK}"`)
  }
})
