// [REVIEW-V2-BATCH3-R2] TrackingReviewPanel + Discovery Detail tracking contract.
//
// 用法（项目现有 harness，纯 TS，node --test / tsx --test 可跑）：
//   cd frontend
//   npx tsx --test src/features/review/__tests__/trackingReviewPanelContract.test.ts
//
// 覆盖 guide TESTS 7/8/9（FIX D 展示层）。现有 harness 无 jsdom / SCSS loader，
// 无法真实渲染 React DOM；组件引用 CSS module（node 无法 import .scss），
// 因此对展示层做源码契约断言，确保 Discovery 关联目标、Scope 关联目标与
// Discovery Detail 创建 payload 的真实逻辑存在且符合产品契约。

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

test('7. TrackingReviewPanel 显示 Discovery target（discoveryId + scope context）', () => {
  const src = read('TrackingReviewPanel.tsx')
  assert.match(src, /t\.trackingType === 'discovery' && t\.discoveryId/, 'discovery 分支存在')
  assert.match(src, /发现 \$\{t\.discoveryId\.slice\(0, 8\)\}/, '显示 discoveryId short identity')
  assert.match(
    src,
    /scopeType && t\.scopeKey \? ` · \$\{t\.scopeType\}\/\$\{t\.scopeKey\}`/,
    '附带 scope type/key evaluation context',
  )
  assert.ok(!/trackingType === 'discovery'.{0,40}\?\s*'-'/.test(src), 'discovery 不得再落入 "-"')
})

test('8. TrackingReviewPanel 仍显示 Scope target', () => {
  const src = read('TrackingReviewPanel.tsx')
  assert.match(src, /t\.trackingType === 'scope'/, 'scope 分支仍存在')
  assert.match(src, /范围 \$\{t\.scopeType \?\? ''\}\/\$\{t\.scopeKey \?\? ''\}/, '仍显示 scope type/key')
})

test('8b. 空态文案符合 Discovery-first 产品路径', () => {
  const src = read('TrackingReviewPanel.tsx')
  assert.match(
    src,
    /在市场发现阶段可将发现、范围或信号加入追踪/,
    '空态文案更新为 Discovery-first 路径',
  )
  assert.ok(!/在「筛选发现」或「个股验证」阶段/.test(src), '移除旧五阶段文案')
})

test('9. Discovery Detail 创建 payload 保持 tracking_type=discovery + discovery_id', () => {
  const src = read('DiscoveryDetail.tsx')
  assert.match(src, /tracking_type: 'discovery'/, 'Discovery 追踪发送 tracking_type=discovery')
  assert.match(src, /discovery_id: discoveryId/, 'Discovery 身份以 discovery_id 传入')
})
