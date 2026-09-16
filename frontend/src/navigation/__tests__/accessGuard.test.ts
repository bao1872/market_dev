// [AccessGuard] - 描述: 权限状态机契约测试（本轮 A 修复的机制回归）
//
// 封面（对应修复要求）：
//   A1 前置条件后到：hydration 未完成 / 未登录时不得消耗唯一触发机会，
//      条件满足后必须仍能进入 needs_revalidation。
//   A2 不无限循环：idle -> loading -> ready 全程只产生一次 needs_revalidation。
//   A3 error 可恢复：error 必须可与 idle/loading 区分（可见错误 + retry），
//      且不允许把非 ready 混同为「无权限 → /forbidden」。
//   A4 普通 member：market_data/self_selection active、research_replay absent
//      仍可放行 /market（mode=any），不得因缺 research_replay 被拦。
//   A5 watchlist：同一 member 的 self_selection 放行；且 pending 绝不等于 forbidden。
//
// 纯函数测试（node --test），不依赖 DOM / React 渲染。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  resolveCapabilityGate,
  resolveProtectedGate,
} from '../accessGuard.ts'

/** 普通 member（qa-normal 等价）：self_selection + market_data active，无 research_replay */
const memberCaps: Record<string, { active?: boolean }> = {
  self_selection: { active: true },
  market_data: { active: true },
}

// =============================================================================
// A1 — 前置条件后到仍能触发补水
// =============================================================================
test('A1: hydration 未完成时不触发补水（decision=hydrating）', () => {
  const d = resolveProtectedGate({
    hydrationStatus: 'hydrating',
    isAuthenticated: true,
    accessStatus: 'idle',
  })
  assert.equal(d, 'hydrating')
})

test('A1: 未登录时不触发补水（decision=unauthenticated）', () => {
  const d = resolveProtectedGate({
    hydrationStatus: 'hydrated',
    isAuthenticated: false,
    accessStatus: 'idle',
  })
  assert.equal(d, 'unauthenticated')
})

test('A1: 条件后到（hydrated + authenticated + idle）→ needs_revalidation', () => {
  // 模拟：第一次求值时 hydration 未完成
  const before = resolveProtectedGate({
    hydrationStatus: 'hydrating',
    isAuthenticated: false,
    accessStatus: 'idle',
  })
  assert.equal(before, 'hydrating')

  // 随后 hydration 完成、登录恢复 —— 旧 revalidatedRef 实现此时已无机会；
  // 状态驱动实现必须重新给出 needs_revalidation。
  const after = resolveProtectedGate({
    hydrationStatus: 'hydrated',
    isAuthenticated: true,
    accessStatus: 'idle',
  })
  assert.equal(after, 'needs_revalidation')
})

// =============================================================================
// A2 — 不无限循环
// =============================================================================
test('A2: idle → loading → ready 只产生一次 needs_revalidation', () => {
  const seq: string[] = [
    resolveProtectedGate({ hydrationStatus: 'hydrated', isAuthenticated: true, accessStatus: 'idle' }),
    resolveProtectedGate({ hydrationStatus: 'hydrated', isAuthenticated: true, accessStatus: 'loading' }),
    resolveProtectedGate({ hydrationStatus: 'hydrated', isAuthenticated: true, accessStatus: 'ready' }),
  ]
  assert.deepEqual(seq, ['needs_revalidation', 'pending', 'ready'])
  assert.equal(seq.filter((d) => d === 'needs_revalidation').length, 1)
  // ready 不是触发态：不得再触发补水（否则形成循环）
  assert.notEqual(seq[2], 'needs_revalidation')
})

// =============================================================================
// A3 — error 可恢复，且不得与 pending/forbidden 混淆
// =============================================================================
test('A3: accessStatus=error → capability 决策为 error（可见错误 + retry），不是黑屏/403', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'error',
    isAdmin: false,
    capabilities: memberCaps,
    required: ['market_data'],
    mode: 'any',
  })
  assert.equal(d, 'error')
  assert.notEqual(d, 'forbidden')
  assert.notEqual(d, 'pending')
})

test('A3: retry 后回到 idle/loading 时仍是 pending（可恢复，不跳 /forbidden）', () => {
  for (const status of ['idle', 'loading'] as const) {
    const d = resolveCapabilityGate({
      accessStatus: status,
      isAdmin: false,
      capabilities: memberCaps,
      required: ['market_data'],
      mode: 'any',
    })
    assert.equal(d, 'pending')
  }
})

// =============================================================================
// A4 — 普通 member 可进入 /market（双 capability，任一即可）
// =============================================================================
test('A4: member + market_data/self_selection → /market 放行', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: false,
    capabilities: memberCaps,
    required: ['self_selection', 'market_data'],
    mode: 'any',
  })
  assert.equal(d, 'allow')
})

test('A4: 仅 self_selection 的 member 也可进入 /market（mode=any）', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: false,
    capabilities: { self_selection: { active: true } },
    required: ['self_selection', 'market_data'],
    mode: 'any',
  })
  assert.equal(d, 'allow')
})

test('A4: 缺 research_replay 不影响 /market（不得因此被拦）', () => {
  assert.equal(memberCaps.research_replay, undefined)
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: false,
    capabilities: memberCaps,
    required: ['self_selection', 'market_data'],
    mode: 'any',
  })
  assert.equal(d, 'allow')
})

// =============================================================================
// A5 — watchlist 放行 + pending 绝不等价 forbidden
// =============================================================================
test('A5: self_selection 可放行 watchlist scope 页面', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: false,
    capabilities: memberCaps,
    required: ['self_selection'],
    mode: 'all',
  })
  assert.equal(d, 'allow')
})

test('A5: 非 ready 永不返回 forbidden（禁止把加载中误判成无权限）', () => {
  for (const status of ['idle', 'loading', 'error'] as const) {
    const d = resolveCapabilityGate({
      accessStatus: status,
      isAdmin: false,
      capabilities: {},
      required: ['market_data'],
      mode: 'any',
    })
    assert.notEqual(d, 'forbidden', `accessStatus=${status} 不得判为 forbidden`)
  }
})

test('A5: ready 且确实无 capability → forbidden（不扩大权限）', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: false,
    capabilities: {},
    required: ['market_data'],
    mode: 'any',
  })
  assert.equal(d, 'forbidden')
})

test('A5: admin 在 ready 时豁免 capability 检查', () => {
  const d = resolveCapabilityGate({
    accessStatus: 'ready',
    isAdmin: true,
    capabilities: {},
    required: ['research_replay'],
    mode: 'all',
  })
  assert.equal(d, 'allow')
})
