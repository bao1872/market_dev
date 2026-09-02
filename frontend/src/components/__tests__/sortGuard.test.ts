// [sortGuard] - 描述: resolveValidSort 排序合同单测（[CHANGE-20260902] B3）
// 用法：node --experimental-strip-types --test src/components/__tests__/sortGuard.test.ts
//
// 覆盖：
// 1. 合法数值列（sortable=true）返回其索引
// 2. 非 sortable 列（文本/股票名/行业）返回 -1
// 3. 未知 key 返回 -1
// 4. 空 sort 返回 -1

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { resolveValidSort } from '../sortGuard.ts'

const columns = [
  { key: 'stock', sortable: false },
  { key: 'change_pct', sortable: true },
  { key: 'price', sortable: true },
  { key: 'industry', sortable: false },
  { key: 'fp_name_a', sortable: false },
  { key: 'fp_score_xyz', sortable: true },
]

test('合法数值列（price/change_pct）返回匹配索引', () => {
  assert.strictEqual(resolveValidSort({ key: 'price', direction: 'desc' }, columns as never), 2)
  assert.strictEqual(resolveValidSort({ key: 'change_pct', direction: 'asc' }, columns as never), 1)
})

test('非 sortable 列（stock/industry/fp_name）返回 -1，不发给 API', () => {
  assert.strictEqual(resolveValidSort({ key: 'stock', direction: 'asc' }, columns as never), -1)
  assert.strictEqual(resolveValidSort({ key: 'industry', direction: 'desc' }, columns as never), -1)
  assert.strictEqual(resolveValidSort({ key: 'fp_name_a', direction: 'asc' }, columns as never), -1)
})

test('未知 sort key 返回 -1（陈旧 URL/preset 残留被忽略）', () => {
  assert.strictEqual(resolveValidSort({ key: 'not_a_column', direction: 'asc' }, columns as never), -1)
})

test('空 sort 返回 -1', () => {
  assert.strictEqual(resolveValidSort(undefined, columns as never), -1)
})
