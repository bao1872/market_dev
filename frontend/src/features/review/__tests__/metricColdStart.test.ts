// [C3] Review 指标冷启动 / readiness 合同测试。
// 纯逻辑测试（无 SCSS / React 渲染），覆盖：
// 1. status=insufficient_history → 展示 rawValue
// 2. value 为空但 raw_ready=true → 仍展示 rawValue（不显示 0 分 / "-"）
// 3. value 有值且 raw_ready=true → 展示 normalized value
// 4. value 空且 raw_ready=false → 非冷启动（value 为 null）
// 5. buildColdStartTitle 含历史观测数与 min_required
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { buildColdStartTitle, resolveMetricColdStart } from '../reviewReadiness.ts'
import type { ReviewMetricPayload } from '../types.ts'

function makePayload(overrides: Partial<ReviewMetricPayload> = {}): ReviewMetricPayload {
  return {
    value: null,
    rawValue: 42.5,
    delta1d: null,
    delta5d: null,
    historyPercentile120d: null,
    crossSectionPercentile: null,
    historyObservationCount: null,
    components: [],
    coverage: null,
    status: 'ready',
    readiness: { raw_ready: false, normalized_ready: false },
    ...overrides,
  }
}

describe('resolveMetricColdStart', () => {
  it('status=insufficient_history 时展示 rawValue（即使 value 也有值）', () => {
    const r = resolveMetricColdStart(
      makePayload({
        status: 'insufficient_history',
        value: 55,
        rawValue: 42.5,
        readiness: { raw_ready: true, normalized_ready: false },
      }),
    )
    assert.equal(r.isCold, true)
    assert.equal(r.displayValue, 42.5)
  })

  it('value 为空但 raw_ready=true 时展示 rawValue，不显示 0 分', () => {
    const r = resolveMetricColdStart(
      makePayload({
        status: 'ready',
        value: null,
        rawValue: 42.5,
        readiness: { raw_ready: true, normalized_ready: false },
      }),
    )
    assert.equal(r.isCold, true)
    assert.equal(r.displayValue, 42.5)
  })

  it('value 为 0 时视为有值，不误判为冷启动', () => {
    const r = resolveMetricColdStart(
      makePayload({
        status: 'ready',
        value: 0,
        rawValue: 42.5,
        readiness: { raw_ready: true, normalized_ready: true },
      }),
    )
    assert.equal(r.isCold, false)
    assert.equal(r.displayValue, 0)
  })

  it('value 有值且 raw_ready=true 时展示 normalized value', () => {
    const r = resolveMetricColdStart(
      makePayload({
        status: 'ready',
        value: 88.3,
        rawValue: 42.5,
        readiness: { raw_ready: true, normalized_ready: true },
      }),
    )
    assert.equal(r.isCold, false)
    assert.equal(r.displayValue, 88.3)
  })

  it('value 为空且 raw_ready=false 时非冷启动（value 为 null，显示占位）', () => {
    const r = resolveMetricColdStart(
      makePayload({ value: null, rawValue: null, readiness: { raw_ready: false, normalized_ready: false } }),
    )
    assert.equal(r.isCold, false)
    assert.equal(r.displayValue, null)
  })

  it('payload 为 null 时返回非冷启动且 displayValue=null', () => {
    const r = resolveMetricColdStart(null)
    assert.equal(r.isCold, false)
    assert.equal(r.displayValue, null)
  })
})

describe('buildColdStartTitle', () => {
  it('包含历史观测数与 min_required 与 readiness.reason', () => {
    const title = buildColdStartTitle(
      makePayload({
        status: 'insufficient_history',
        historyObservationCount: 30,
        readiness: { raw_ready: true, normalized_ready: false, min_required: 60, reason: 'history < 60' },
      }),
    )
    assert.match(title, /历史不足/)
    assert.match(title, /30/)
    assert.match(title, /60/)
    assert.match(title, /history < 60/)
  })
})
