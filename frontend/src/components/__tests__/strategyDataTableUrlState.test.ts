// [StrategyDataTable] - 描述: URL 状态同步源码契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/strategyDataTableUrlState.test.ts
//
// 覆盖：
//   1. StrategyDataTable 源码必须 import/use decodeScreenerUrlState 和 encodeScreenerUrlState
//   2. StrategyDataTable 源码必须有 hydration guard
//   3. decodeScreenerUrlState 对 malformed filters 不抛错并返回 filters=[]
//   4. encodeScreenerUrlState 不写 selectedKeys/activeRunId/rows/results

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import {
  decodeScreenerUrlState,
  encodeScreenerUrlState,
  type ScreenerUrlState,
} from '../screenerUrlState.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const source = readFileSync(join(__dirname, '../StrategyDataTable.tsx'), 'utf8')

test('StrategyDataTable 源码必须 import 并使用 decodeScreenerUrlState', () => {
  assert.ok(source.includes('decodeScreenerUrlState'), '应引用 decodeScreenerUrlState')
})

test('StrategyDataTable 源码必须 import 并使用 encodeScreenerUrlState', () => {
  assert.ok(source.includes('encodeScreenerUrlState'), '应引用 encodeScreenerUrlState')
})

test('StrategyDataTable 源码必须有 hydration guard', () => {
  const hasGuard =
    source.includes('urlHydratedRef') ||
    /isHydrated|hydratedRef|hasHydrated/i.test(source)
  assert.ok(hasGuard, '应有 hydration guard 防止首屏用默认 state 覆盖 URL')
})

test('decodeScreenerUrlState 对 malformed filters 不抛错，并返回 filters=[]', () => {
  const validKeys = new Set(['change_pct', 'bb.position'])

  const nonJson = new URLSearchParams()
  nonJson.set('filters', 'not-json')
  assert.deepStrictEqual(decodeScreenerUrlState(nonJson, validKeys).filters, [])

  const notArray = new URLSearchParams()
  notArray.set('filters', JSON.stringify({ key: 'change_pct', op: 'gte' }))
  assert.deepStrictEqual(decodeScreenerUrlState(notArray, validKeys).filters, [])

  const numberJson = new URLSearchParams()
  numberJson.set('filters', '123')
  assert.deepStrictEqual(decodeScreenerUrlState(numberJson, validKeys).filters, [])
})

test('encodeScreenerUrlState 不写 selectedKeys/activeRunId/rows/results', () => {
  const state: ScreenerUrlState = {
    strategy: 'dsa_selector',
    keyword: '新能源',
    // @ts-expect-error 测试非法字段是否被过滤
    selectedKeys: ['a', 'b'],
    activeRunId: 'run-123',
    rows: [{ id: 1 }],
    results: [{ id: 2 }],
  }
  const params = encodeScreenerUrlState(state)
  assert.equal(params.has('selectedKeys'), false)
  assert.equal(params.has('activeRunId'), false)
  assert.equal(params.has('rows'), false)
  assert.equal(params.has('results'), false)
})
