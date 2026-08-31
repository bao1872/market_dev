// [T1-CORRECTION-01 / Fix A] ScopeMemberAttributionPanel 用户可见术语收口回归测试。
//
// 只扫描正式 render/source surface（ScopeMemberAttributionPanel.tsx 源文件），
// 不扫描整个 repo、PRD、backend comments。
//
// 目标：
// - 用户可见层不再渲染 T1 之前与 SSOT 冲突的旧术语；
// - canonical payload key 必须保留（展示改写不得吃掉 key）。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'ScopeMemberAttributionPanel.tsx')
const src = readFileSync(SRC, 'utf8')

// 用户可见旧术语（与 T1 SSOT 冲突，必须已从面板移除）
const FORBIDDEN_DISPLAY = [
  '资金偏向贡献',
  '成交额加权收益基准',
  '等权收益基准',
  '原始集中度',
  '标准化集中度',
]

test('T1CORR-A1: ScopeMemberAttributionPanel 用户可见层不再含旧术语', () => {
  for (const w of FORBIDDEN_DISPLAY) {
    assert.ok(
      !src.includes(w),
      `ScopeMemberAttributionPanel 不得再渲染用户可见旧术语 "${w}"`,
    )
  }
})

// canonical payload key 必须保留（展示文案改写不得删除 key / 改 key）
test('T1CORR-A2: ScopeMemberAttributionPanel canonical keys 仍保留', () => {
  for (const k of [
    'tilt_contribution',
    'return_1d',
    'canonical_aw_return',
    'canonical_ew_return',
    'raw_hhi',
    'normalized_hhi',
  ]) {
    assert.ok(src.includes(k), `ScopeMemberAttributionPanel 必须保留 canonical key "${k}"`)
  }
})
