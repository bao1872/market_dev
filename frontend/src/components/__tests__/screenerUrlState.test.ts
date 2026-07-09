// [ScreenerUrlState] - 描述: 趋势选股 URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/screenerUrlState.test.ts
//
// 覆盖：
//   1. encode/decode 往返一致（strategy/keyword/sort/filters/page/pageSize）
//   2. 只保存 key/op/value/value2，不保存 rows/selectedKeys/activeRunId/results
//   3. decode 时丢弃当前 columns 中不存在的陈旧 filter key 和 sort key
//   4. 空/非法 filter JSON 优雅回退

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  encodeScreenerUrlState,
  decodeScreenerUrlState,
  type ScreenerUrlState,
} from '../screenerUrlState.ts'

const baseState: ScreenerUrlState = {
  strategy: 'dsa_selector',
  keyword: '新能源',
  sort: { key: 'change_pct', direction: 'desc' },
  filters: [
    { key: 'change_pct', op: 'gte', value: 3 },
    { key: 'bb.position', op: 'between', value: 0.2, value2: 0.8 },
  ],
  page: 3,
  pageSize: 50,
}

const validKeys = new Set(['change_pct', 'bb.position', 'volume_confirm'])

test('encode/decode 往返一致', () => {
  const params = encodeScreenerUrlState(baseState)
  const decoded = decodeScreenerUrlState(params, validKeys)
  assert.deepStrictEqual(decoded, baseState)
})

test('encode 不把 selectedKeys/activeRunId/rows/results 写入 URL', () => {
  const state: ScreenerUrlState = {
    ...baseState,
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
  const decoded = decodeScreenerUrlState(params, validKeys) as Record<string, unknown>
  assert.equal(decoded.selectedKeys, undefined)
  assert.equal(decoded.activeRunId, undefined)
  assert.equal(decoded.rows, undefined)
  assert.equal(decoded.results, undefined)
})

test('decode 丢弃陈旧的 filter key 和 sort key', () => {
  const params = new URLSearchParams()
  params.set('strategy', 'dsa_selector')
  params.set('sort', 'old_metric')
  params.set('dir', 'asc')
  params.set('filters', JSON.stringify([
    { key: 'old_metric', op: 'gte', value: 1 },
    { key: 'change_pct', op: 'gte', value: 3 },
  ]))
  const decoded = decodeScreenerUrlState(params, validKeys)
  assert.equal(decoded.sort, undefined)
  assert.deepStrictEqual(decoded.filters, [{ key: 'change_pct', op: 'gte', value: 3 }])
})

test('decode 非法 filters JSON 时回退为空数组', () => {
  const params = new URLSearchParams()
  params.set('filters', 'not-json')
  const decoded = decodeScreenerUrlState(params, validKeys)
  assert.deepStrictEqual(decoded.filters, [])
})

test('encode 默认 page=1 时省略 page，pageSize 与初始一致时省略', () => {
  const state: ScreenerUrlState = {
    strategy: 'dsa_selector',
    keyword: '',
    filters: [],
    page: 1,
    pageSize: 50,
  }
  const params = encodeScreenerUrlState(state)
  assert.equal(params.has('page'), false)
  assert.equal(params.has('page_size'), false)
  assert.equal(params.get('strategy'), 'dsa_selector')
})
