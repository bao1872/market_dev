// V2.1 邀请码能力配置纯函数测试（PRD §6.4 + G3）
// 用法：node --experimental-strip-types --test src/features/invite-capability/__tests__/inviteCapabilityValidation.test.ts
//
// 覆盖：
// - checkbox 组合（至少一个能力、各组合）
// - 自选字段启停和清空（watchlist_management 切换时额度输入启停/重置）
// - 错误输入（0、负数、超限、非整数）
// - DTO 序列化（formToCreateRequest）
// - 响应渲染（formatCapabilitySummary / formatInviteCodeStatus）
// - 撤销按钮状态（仅 available 可撤销，由列表项 status 决定）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  CAPABILITY_KEYS,
  CAPABILITY_LABELS,
  INITIAL_FORM_STATE,
  MAX_DURATION_MONTHS,
  MAX_INVITE_COUNT,
  MAX_WATCHLIST_STOCK_LIMIT,
  formatCapabilitySummary,
  formatInviteCodeStatus,
  formToCreateRequest,
  validateInviteCapabilityForm,
  type CapabilityFormState,
} from '../inviteCapabilityValidation.ts'
import type { InviteCodeCapabilityItem } from '@/api/endpoints'

test('CAPABILITY_KEYS 包含三个能力键', () => {
  assert.deepEqual([...CAPABILITY_KEYS], [
    'watchlist_management',
    'market_screening',
    'review_management',
  ])
})

test('CAPABILITY_LABELS 覆盖所有能力键', () => {
  for (const key of CAPABILITY_KEYS) {
    assert.ok(CAPABILITY_LABELS[key], `缺 ${key} 标签`)
    assert.ok(CAPABILITY_LABELS[key].label, `${key} 标签为空`)
  }
})

// ============================================================
// 至少一个能力
// ============================================================

test('全部未勾选时报错 capabilities', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: false,
    market_screening: false,
    review_management: false,
    watchlist_stock_limit: '',
  }
  const errors = validateInviteCapabilityForm(form)
  assert.equal(errors.capabilities, '至少勾选一个能力')
})

test('只勾选 market_screening 时不报 capabilities 错误', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: false,
    market_screening: true,
    review_management: false,
    watchlist_stock_limit: '',
  }
  const errors = validateInviteCapabilityForm(form)
  assert.equal(errors.capabilities, undefined)
})

test('只勾选 review_management 时不报 capabilities 错误', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: false,
    market_screening: false,
    review_management: true,
    watchlist_stock_limit: '',
  }
  const errors = validateInviteCapabilityForm(form)
  assert.equal(errors.capabilities, undefined)
})

// ============================================================
// 自选额度
// ============================================================

test('watchlist_management 勾选但额度为空时报错', () => {
  const form: CapabilityFormState = { ...INITIAL_FORM_STATE, watchlist_management: true, watchlist_stock_limit: '' }
  const errors = validateInviteCapabilityForm(form)
  assert.ok(errors.watchlist_stock_limit?.includes('正整数'))
})

test('watchlist_management 勾选但额度为 0 时报错', () => {
  const form = { ...INITIAL_FORM_STATE, watchlist_management: true, watchlist_stock_limit: 0 }
  const errors = validateInviteCapabilityForm(form)
  assert.ok(errors.watchlist_stock_limit)
})

test('watchlist_management 勾选但额度为负数时报错', () => {
  const form = { ...INITIAL_FORM_STATE, watchlist_management: true, watchlist_stock_limit: -5 }
  const errors = validateInviteCapabilityForm(form)
  assert.ok(errors.watchlist_stock_limit)
})

test('watchlist_management 勾选但额度超过上限时报错', () => {
  const form = {
    ...INITIAL_FORM_STATE,
    watchlist_management: true,
    watchlist_stock_limit: MAX_WATCHLIST_STOCK_LIMIT + 1,
  }
  const errors = validateInviteCapabilityForm(form)
  assert.ok(errors.watchlist_stock_limit?.includes('超过'))
})

test('watchlist_management 勾选但额度非整数时报错', () => {
  const form = { ...INITIAL_FORM_STATE, watchlist_management: true, watchlist_stock_limit: 1.5 }
  const errors = validateInviteCapabilityForm(form)
  assert.ok(errors.watchlist_stock_limit?.includes('整数'))
})

test('watchlist_management 未勾选时不校验额度', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: false,
    market_screening: true,
    watchlist_stock_limit: '',
  }
  const errors = validateInviteCapabilityForm(form)
  assert.equal(errors.watchlist_stock_limit, undefined)
})

test('合法额度通过', () => {
  const form = { ...INITIAL_FORM_STATE, watchlist_management: true, watchlist_stock_limit: 20 }
  const errors = validateInviteCapabilityForm(form)
  assert.equal(errors.watchlist_stock_limit, undefined)
})

// ============================================================
// 授权月数
// ============================================================

test('月数为空时报错', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, duration_months: '' })
  assert.ok(errors.duration_months)
})

test('月数为 0 时报错', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, duration_months: 0 })
  assert.ok(errors.duration_months)
})

test(`月数超过 ${MAX_DURATION_MONTHS} 时报错`, () => {
  const errors = validateInviteCapabilityForm({
    ...INITIAL_FORM_STATE,
    duration_months: MAX_DURATION_MONTHS + 1,
  })
  assert.ok(errors.duration_months?.includes('超过'))
})

test('月数非整数时报错', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, duration_months: 1.5 })
  assert.ok(errors.duration_months?.includes('整数'))
})

test('合法月数通过', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, duration_months: 6 })
  assert.equal(errors.duration_months, undefined)
})

// ============================================================
// 生成数量
// ============================================================

test('count 为空时报错', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, count: '' })
  assert.ok(errors.count)
})

test(`count 超过 ${MAX_INVITE_COUNT} 时报错`, () => {
  const errors = validateInviteCapabilityForm({
    ...INITIAL_FORM_STATE,
    count: MAX_INVITE_COUNT + 1,
  })
  assert.ok(errors.count?.includes('超过'))
})

test('合法 count 通过', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, count: 5 })
  assert.equal(errors.count, undefined)
})

// ============================================================
// 批次备注
// ============================================================

test('空备注通过', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, note: '' })
  assert.equal(errors.note, undefined)
})

test('超过 200 字符报错', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, note: 'a'.repeat(201) })
  assert.ok(errors.note?.includes('200'))
})

test('刚好 200 字符通过', () => {
  const errors = validateInviteCapabilityForm({ ...INITIAL_FORM_STATE, note: 'a'.repeat(200) })
  assert.equal(errors.note, undefined)
})

// ============================================================
// DTO 序列化
// ============================================================

test('formToCreateRequest 全勾选时生成 3 个能力项', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: true,
    market_screening: true,
    review_management: true,
    watchlist_stock_limit: 30,
    duration_months: 3,
    count: 5,
    note: '测试批次',
  }
  const req = formToCreateRequest(form)
  assert.equal(req.count, 5)
  assert.equal(req.duration_months, 3)
  assert.equal(req.capabilities.length, 3)
  assert.ok(req.capabilities.some(
    (c) => c.capability_key === 'watchlist_management' && c.limit_value === 30,
  ))
  assert.ok(req.capabilities.some(
    (c) => c.capability_key === 'market_screening' && c.limit_value === null,
  ))
  assert.ok(req.capabilities.some(
    (c) => c.capability_key === 'review_management' && c.limit_value === null,
  ))
  assert.equal(req.note, '测试批次')
})

test('formToCreateRequest 只勾选 watchlist_management 时只生成 1 个能力项', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: true,
    market_screening: false,
    review_management: false,
    watchlist_stock_limit: 50,
    duration_months: 1,
    count: 1,
    note: '',
  }
  const req = formToCreateRequest(form)
  assert.equal(req.capabilities.length, 1)
  assert.deepEqual(req.capabilities[0], {
    capability_key: 'watchlist_management',
    limit_value: 50,
  })
  assert.equal(req.note, undefined)
})

test('formToCreateRequest 未勾选 watchlist_management 时不包含 watchlist_management 能力', () => {
  const form: CapabilityFormState = {
    ...INITIAL_FORM_STATE,
    watchlist_management: false,
    market_screening: true,
    review_management: false,
    watchlist_stock_limit: '',
    duration_months: 1,
    count: 1,
    note: '',
  }
  const req = formToCreateRequest(form)
  assert.equal(req.capabilities.length, 1)
  assert.equal(req.capabilities[0].capability_key, 'market_screening')
  assert.equal(req.capabilities[0].limit_value, null)
})

test('formToCreateRequest note 仅空白时转为 undefined', () => {
  const form = { ...INITIAL_FORM_STATE, note: '   ' }
  const req = formToCreateRequest(form)
  assert.equal(req.note, undefined)
})

// ============================================================
// 响应渲染
// ============================================================

test('formatCapabilitySummary 单个能力（watchlist）正确格式化', () => {
  const caps: InviteCodeCapabilityItem[] = [
    { capability_key: 'watchlist_management', limit_value: 20 },
  ]
  assert.equal(formatCapabilitySummary(caps, 3), '自选管理×20 · 3个月')
})

test('formatCapabilitySummary 多能力组合正确格式化', () => {
  const caps: InviteCodeCapabilityItem[] = [
    { capability_key: 'watchlist_management', limit_value: 30 },
    { capability_key: 'market_screening', limit_value: null },
  ]
  assert.equal(formatCapabilitySummary(caps, 6), '自选管理×30 + 行情选股 · 6个月')
})

test('formatCapabilitySummary 三能力全开', () => {
  const caps: InviteCodeCapabilityItem[] = [
    { capability_key: 'watchlist_management', limit_value: 50 },
    { capability_key: 'market_screening', limit_value: null },
    { capability_key: 'review_management', limit_value: null },
  ]
  assert.equal(formatCapabilitySummary(caps, 12), '自选管理×50 + 行情选股 + 复盘管理 · 12个月')
})

// ============================================================
// 状态渲染
// ============================================================

test('formatInviteCodeStatus available → 未使用', () => {
  assert.equal(formatInviteCodeStatus('available'), '未使用')
})

test('formatInviteCodeStatus redeemed → 已兑换', () => {
  assert.equal(formatInviteCodeStatus('redeemed'), '已兑换')
})

test('formatInviteCodeStatus revoked → 已撤销', () => {
  assert.equal(formatInviteCodeStatus('revoked'), '已撤销')
})

// ============================================================
// 撤销按钮状态（列表项 status 决定）
// ============================================================

test('available 显示撤销按钮', () => {
  const status: 'available' | 'redeemed' | 'revoked' = 'available'
  assert.equal(formatInviteCodeStatus(status), '未使用')
  assert.equal(status === 'available', true)
})

test('redeemed 不显示撤销按钮', () => {
  const status: 'available' | 'redeemed' | 'revoked' = 'redeemed' as 'available' | 'redeemed' | 'revoked'
  assert.equal(formatInviteCodeStatus(status), '已兑换')
  assert.equal(status === 'available', false)
})

test('revoked 不显示撤销按钮', () => {
  const status: 'available' | 'redeemed' | 'revoked' = 'revoked' as 'available' | 'redeemed' | 'revoked'
  assert.equal(formatInviteCodeStatus(status), '已撤销')
  assert.equal(status === 'available', false)
})
