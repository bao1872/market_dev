// [REVIEW-UX-EXPERIMENT-READINESS-01 Slice A] 总成交额（百亿元）展示契约。
//
// 覆盖 A2 Unit Gate 之后的展示换算与 A4 的 null 语义：
// - 单位换算 display = raw / 10^10（1 百亿元 = 10^10 元）
// - null / undefined / NaN → '—'，绝不落 0
// - 真实 0 → '0.00'，与 unavailable 的 '—' 严格区分
// - 极小正值不得显示为 '0.00'（防假零）
// - 空 scopeName 占位不得是 UUID
//
// 纯函数测试：不连后端、不渲染 React，可被 tsx --test 直接运行。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  NULL_DISPLAY,
  AMOUNT_UNIT_LABEL,
  RAW_YUAN_PER_BAI_YI_YUAN,
  UNNAMED_SCOPE_LABEL,
  formatAmountInBaiYiYuan,
} from '../reviewFormat'

test('A2 常量：1 百亿元 = 10^10 元', () => {
  assert.equal(RAW_YUAN_PER_BAI_YI_YUAN, 10_000_000_000)
  assert.equal(AMOUNT_UNIT_LABEL, '百亿元')
})

test('A2 单位换算：raw 元 → 百亿元', () => {
  assert.equal(formatAmountInBaiYiYuan(10_000_000_000), '1.00')
  assert.equal(formatAmountInBaiYiYuan(1_000_000_000), '0.10')
  // 真实生产量级：402 成员 concept，trade_date 2026-07-29
  // （experiments/review_real_market_acceptance/canonical_compositions.jsonl）
  assert.equal(formatAmountInBaiYiYuan(312_834_079_356), '31.28')
})

test('A4 null / undefined / NaN → —（绝不落 0）', () => {
  assert.equal(formatAmountInBaiYiYuan(null), NULL_DISPLAY)
  assert.equal(formatAmountInBaiYiYuan(undefined), NULL_DISPLAY)
  assert.equal(formatAmountInBaiYiYuan(Number.NaN), NULL_DISPLAY)
  assert.equal(NULL_DISPLAY, '—')
})

test('A4 真实 0 → 0.00，与 unavailable 的 — 严格区分', () => {
  assert.equal(formatAmountInBaiYiYuan(0), '0.00')
  assert.notEqual(formatAmountInBaiYiYuan(0), NULL_DISPLAY)
})

test('A4 极小正值不得显示为 0.00（防假零）', () => {
  // 1e6 元 = 0.0001 百亿元：真实有成交，但按 2 位小数舍入会变成 0.00
  assert.equal(formatAmountInBaiYiYuan(1_000_000), '<0.01')
  assert.notEqual(formatAmountInBaiYiYuan(1_000_000), '0.00')
})

test('A3 空 scopeName 占位不是内部 UUID', () => {
  assert.equal(UNNAMED_SCOPE_LABEL, '未命名板块')
  const uuidLike = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
  assert.equal(uuidLike.test(UNNAMED_SCOPE_LABEL), false)
})
