// [Phase 5B / B2] Review 五正式阶段契约测试（纯 TS，node --test / tsx --test 可跑）。
// 覆盖：
//   1. 正式导航 STAGES 仅含五阶段，不含 auction（auction 降级为 auxiliary entry）。
//   2. urlState 仍接受 auction 深链（auxiliary 入口保留）。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { REVIEW_FORMAL_STAGES, normalizeStage } from '../urlState'

test('正式 Review 阶段仅含五阶段，不含 auction', () => {
  const formal = [...REVIEW_FORMAL_STAGES]
  assert.deepStrictEqual(formal, [
    'scan',
    'signals',
    'attribution',
    'validation',
    'tracking',
  ])
  assert.ok(!formal.includes('auction'), 'auction 不得作为正式第六阶段')
})

test('auction 深链仍可解析（auxiliary entry 保留）', () => {
  assert.strictEqual(normalizeStage('auction'), 'auction')
  // 正式五阶段保持有效
  for (const s of ['scan', 'signals', 'attribution', 'validation', 'tracking']) {
    assert.strictEqual(normalizeStage(s), s)
  }
  // 非法阶段回退默认
  assert.strictEqual(normalizeStage('bogus'), 'scan')
})
