// [REVIEW-PRODUCT-CLOSURE-01 Phase C] 成员身份目录展示合同测试（纯 TS，tsx --test 可跑）。
//
// 覆盖：
// - displayMember：目录 name+symbol → "名称 · 代码"；仅 symbol → symbol；
//   仅 name → name；目录缺失 → UUID 兜底（仅 title/技术 hover 保留 UUID）
// - Leadership 与 Attribution 必须使用 SAME display owner（displayMember）
// - directory miss 时 UUID 回退是最终兜底（Attribution 不得私自回退 payload member_name）
// - null/unavailable 语义不变
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  displayMember,
  memberName,
  type MemberDirectory,
} from '../reviewFormat'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

const UUID = '01060b6b-82cb-4704-88c7-34c67c5ea82c'

const DIRECTORY: MemberDirectory = {
  [UUID]: { symbol: '300331', name: '苏大维格' },
  '0473e2f3-91b4-4526-abcc-a5e12cfb9fc1': { symbol: '688356', name: '某研科技' },
}

// ============================================================
// displayMember — 成员展示唯一 owner
// ============================================================

test('PC1. name+symbol 齐全 → "名称 · 代码"', () => {
  assert.equal(displayMember(UUID, DIRECTORY), '苏大维格 · 300331')
  assert.equal(displayMember('0473e2f3-91b4-4526-abcc-a5e12cfb9fc1', DIRECTORY), '某研科技 · 688356')
})

test('PC2. 仅 symbol → symbol；仅 name → name', () => {
  const symbolOnly: MemberDirectory = { [UUID]: { symbol: '300331', name: '' } }
  assert.equal(displayMember(UUID, symbolOnly), '300331')
  const nameOnly: MemberDirectory = { [UUID]: { symbol: '', name: '苏大维格' } }
  assert.equal(displayMember(UUID, nameOnly), '苏大维格')
})

test('PC3. 目录缺失 → UUID 兜底（最终 fallback）', () => {
  assert.equal(displayMember(UUID, null), UUID)
  assert.equal(displayMember(UUID, undefined), UUID)
  assert.equal(displayMember(UUID, {}), UUID)
  assert.equal(displayMember('not-in-directory', DIRECTORY), 'not-in-directory')
})

test('PC4. 目录条目为空字符串 → UUID 兜底', () => {
  const empty: MemberDirectory = { [UUID]: { symbol: '', name: '' } }
  assert.equal(displayMember(UUID, empty), UUID)
})

test('PC5. 展示结果绝不等于裸 UUID（目录存在时主展示不出现 UUID）', () => {
  const out = displayMember(UUID, DIRECTORY)
  assert.ok(!out.includes(UUID), `主展示不得含 UUID: ${out}`)
})

// ============================================================
// memberName — 纯 evidence name 提取工具（保留原语义）
// ============================================================

test('PC6. memberName 原语义保留（目录未命中时）', () => {
  assert.equal(memberName({ member_id: UUID, member_name: UUID }), UUID)
  assert.equal(memberName({ member_id: UUID, member_name: '真实名称' }), '真实名称')
  assert.equal(memberName({ member_id: UUID }), UUID)
})

// ============================================================
// 面板接入 — Leadership 与 Attribution 必须 SAME display owner
// ============================================================

const LEADERSHIP_SRC = read('ScopeLeadershipPanel.tsx')
const ATTRIBUTION_SRC = read('ScopeMemberAttributionPanel.tsx')

test('PC7. Leadership 面板接入 displayMember（不展示裸 UUID）', () => {
  assert.ok(LEADERSHIP_SRC.includes('displayMember(id, directory)'), '龙头 chip 走 displayMember')
  assert.ok(LEADERSHIP_SRC.includes('title={id}'), 'UUID 仅保留在 title 技术 hover')
  assert.doesNotMatch(LEADERSHIP_SRC, /\{ids\.map\(\(id\) => \(\s*<span[^>]*>\{id\}<\/span>/)
})

test('PC8. Attribution 面板接入 displayMember（与 Leadership SAME owner）', () => {
  assert.ok(
    ATTRIBUTION_SRC.includes('displayMember(m.member_id, directory)'),
    'Attribution 成员行走 displayMember（与 Leadership 同 owner）',
  )
  // 不得私自使用另一套 identity owner
  assert.ok(
    !ATTRIBUTION_SRC.includes('displayMemberEvidence'),
    'Attribution 不得调用已删除的 displayMemberEvidence（禁止第二套 fallback）',
  )
  assert.ok(ATTRIBUTION_SRC.includes('title={String(m.member_id)}'), 'UUID 仅保留在 title 技术 hover')
})

test('PC9. Attribution directory miss → UUID（不回退 payload member_name）', () => {
  // 即使 evidence 携带 payload member_name，目录缺失时也必须回退 UUID，不伪造身份
  assert.equal(displayMember(UUID, null), UUID)
  assert.equal(displayMember(UUID, undefined), UUID)
  assert.equal(displayMember(UUID, {}), UUID)
})

test('PC10. Detail 工作区把 memberDirectory 传给 Leadership + Attribution', () => {
  const detail = read('ScopeDetailWorkspace.tsx')
  assert.ok(detail.includes('memberDirectory={detail.data?.memberDirectory}'))
})

test('PC11. 详情响应契约含 memberDirectory（types.ts）', () => {
  const types = read('types.ts')
  assert.ok(types.includes('memberDirectory: Record<string, { symbol: string; name: string }>'))
})