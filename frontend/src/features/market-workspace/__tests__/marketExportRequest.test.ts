import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { buildMarketExportRequest } from '../marketWorkspaceUrlState.ts'

// ===== C3：导出列由服务端固定（股票名称 + 股票代码），前端不再发送 visible_columns =====

test('C3: buildMarketExportRequest omits visible_columns and carries only query semantics', () => {
  const body = buildMarketExportRequest({
    scope: 'market',
    keyword: null,
    industry: '银行',
    concept: null,
    sortBy: null,
    sortDesc: false,
    filters: [],
  })
  assert.equal(
    'visible_columns' in body,
    false,
    'server owns export columns; client must not send visible_columns',
  )
  assert.equal(body.scope, 'market')
  assert.equal(body.industry, '银行')
  assert.equal(body.concept, null)
  for (const key of [
    'scope',
    'keyword',
    'industry',
    'concept',
    'state',
    'fp_filter',
    'fp_sort',
    'sort',
    'stock_name',
    'stock_name_op',
  ] as const) {
    assert.ok(key in body, `request must include ${key}`)
  }
})

test('C3: fp filters serialize into fp_filter (same source as /market/stocks)', () => {
  const body = buildMarketExportRequest({
    scope: 'market',
    keyword: null,
    industry: null,
    concept: null,
    sortBy: null,
    sortDesc: false,
    filters: [{ key: 'fp_pe', operator: 'gt', value: 10 }] as unknown as never,
  })
  assert.equal('visible_columns' in body, false)
  assert.ok(
    body.fp_filter != null && body.fp_filter.length > 0,
    'fp filter must be serialized into fp_filter',
  )
})
