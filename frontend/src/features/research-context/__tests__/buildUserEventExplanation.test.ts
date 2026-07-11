// [buildUserEventExplanation] - 描述: 用户事件解释纯函数契约测试
// 用法：node --experimental-strip-types --test src/features/research-context/__tests__/buildUserEventExplanation.test.ts
//
// 覆盖：
//   1. null eventDetail → hasEvent=false
//   2. 完整 eventDetail → 提取 eventTime/eventType/eventLabel/price/evidence
//   3. event_type 未知时 eventLabel 回退为原 eventType
//   4. payload.facts 数组提取价格（current_price key）
//   5. payload 顶层字段提取价格（current_price/price/last_price/close_price）
//   6. payload.text_content 中"现价：xxx"正则提取
//   7. evidence 只包含 text_content 和 summary
//   8. instrument_id 不匹配 → instrumentMismatch=true
//   9. instrument_id 匹配 → instrumentMismatch=false
//  10. currentInstrumentId 为空时 → instrumentMismatch=false（不校验）
//  11. event.instrument_id 为空时 → instrumentMismatch=false
//  12. payload 无任何白名单字段 → price=null, evidence=[]

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { buildUserEventExplanation } from '../buildUserEventExplanation.ts'
import type { StrategyEventDetail } from '../../../api/endpoints.ts'

function makeEventFixture(overrides?: {
  payload?: Record<string, unknown>
  instrument_id?: string
  event_type?: string
  event_time?: string
}): StrategyEventDetail {
  return {
    id: 'evt-1',
    event_key: 'key-1',
    strategy_version_id: 'sv-1',
    instrument_id: overrides?.instrument_id ?? 'inst-1',
    event_type: overrides?.event_type ?? 'bb_upper_touch',
    event_time: overrides?.event_time ?? '2026-07-10T14:30:00+08:00',
    logical_entity_id: null,
    schema_version: 1,
    payload: overrides?.payload ?? {},
    created_at: '2026-07-10T14:30:01+08:00',
    snapshot: {},
  }
}

test('null eventDetail → hasEvent=false', () => {
  const result = buildUserEventExplanation({ eventDetail: null })
  assert.equal(result.hasEvent, false)
  assert.equal(result.eventTime, null)
  assert.equal(result.eventType, null)
  assert.equal(result.eventLabel, null)
  assert.equal(result.price, null)
  assert.deepEqual(result.evidence, [])
  assert.equal(result.instrumentMismatch, false)
})

test('undefined eventDetail → hasEvent=false', () => {
  const result = buildUserEventExplanation({})
  assert.equal(result.hasEvent, false)
})

test('完整 eventDetail → 提取 eventTime/eventType/eventLabel', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ event_type: 'bb_upper_touch' }),
  })
  assert.equal(result.hasEvent, true)
  assert.equal(result.eventTime, '2026-07-10T14:30:00+08:00')
  assert.equal(result.eventType, 'bb_upper_touch')
  assert.equal(result.eventLabel, '价格触及近期波动上沿')
})

test('event_type 未知时 eventLabel 回退为原 eventType', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ event_type: 'unknown_event_type' }),
  })
  assert.equal(result.eventType, 'unknown_event_type')
  assert.equal(result.eventLabel, 'unknown_event_type')
})

test('payload.facts 数组提取价格（current_price key）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: {
        facts: [
          { key: 'other', value: 'ignore' },
          { key: 'current_price', value: 10.5 },
        ],
      },
    }),
  })
  assert.equal(result.price, '10.5')
})

test('payload.facts 数组提取价格（现价 key）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: {
        facts: [{ key: '现价', value: '9.8' }],
      },
    }),
  })
  assert.equal(result.price, '9.8')
})

test('payload 顶层字段提取价格（current_price 优先）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { current_price: 12.3, price: 11.0 },
    }),
  })
  assert.equal(result.price, '12.3')
})

test('payload 顶层字段提取价格（price 回退）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { price: 11.0 },
    }),
  })
  assert.equal(result.price, '11')
})

test('payload.text_content 中"现价：xxx"正则提取', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { text_content: '触发时现价: 10.5，已突破上轨' },
    }),
  })
  assert.equal(result.price, '10.5')
})

test('evidence 只包含 text_content 和 summary', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: {
        text_content: '价格触及上轨',
        summary: '技术形态转强',
        internal_field: 'should_not_appear',
      },
    }),
  })
  assert.deepEqual(result.evidence, ['价格触及上轨', '技术形态转强'])
})

test('evidence 去重（text_content 与 summary 相同时只保留一个）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { text_content: '相同内容', summary: '相同内容' },
    }),
  })
  assert.deepEqual(result.evidence, ['相同内容'])
})

test('instrument_id 不匹配 → instrumentMismatch=true', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ instrument_id: 'inst-A' }),
    currentInstrumentId: 'inst-B',
  })
  assert.equal(result.instrumentMismatch, true)
})

test('instrument_id 匹配 → instrumentMismatch=false', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ instrument_id: 'inst-A' }),
    currentInstrumentId: 'inst-A',
  })
  assert.equal(result.instrumentMismatch, false)
})

test('currentInstrumentId 为空时 → instrumentMismatch=false（不校验）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ instrument_id: 'inst-A' }),
    currentInstrumentId: null,
  })
  assert.equal(result.instrumentMismatch, false)
})

test('currentInstrumentId 为 undefined 时 → instrumentMismatch=false', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ instrument_id: 'inst-A' }),
  })
  assert.equal(result.instrumentMismatch, false)
})

test('event.instrument_id 为空时 → instrumentMismatch=false', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ instrument_id: '' }),
    currentInstrumentId: 'inst-A',
  })
  assert.equal(result.instrumentMismatch, false)
})

test('payload 无任何白名单字段 → price=null, evidence=[]', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { internal_data: 'xxx', algorithm_param: 0.5 },
    }),
  })
  assert.equal(result.price, null)
  assert.deepEqual(result.evidence, [])
})

test('payload.facts 无白名单 key 时 → price=null（不从 facts 提取）', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({
      payload: { facts: [{ key: 'other', value: 'ignore' }] },
    }),
  })
  assert.equal(result.price, null)
})

test('STOCK_SNAPSHOT_SHARE 事件类型有通俗文案', () => {
  const result = buildUserEventExplanation({
    eventDetail: makeEventFixture({ event_type: 'STOCK_SNAPSHOT_SHARE' }),
  })
  assert.equal(result.eventLabel, '个股快照')
})
