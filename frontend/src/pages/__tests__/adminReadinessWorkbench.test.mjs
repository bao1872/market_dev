// [Commit H] 盘后就绪工作台渲染逻辑测试
// 用法：node --test src/pages/__tests__/adminReadinessWorkbench.test.mjs
//
// 验证：
// - 数据生产中心新增"盘后就绪"tab，渲染 AdminReadinessWorkbench
// - 工作台消费 GET /v1/admin/readiness/{trade_date}（Commit G 正式发布读模型）
// - 展示九节点状态 / 闭包 / governance（lineage / stale / unmatched / degraded）
// - loading / error / empty 状态均有明确处理
// - 用户侧只消费正式 publication（本页只读，不触发写操作）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminDataProductionPage.tsx')
const WORKBENCH_PATH = join(
  __dirname,
  '..',
  '..',
  'features',
  'product-readiness',
  'AdminReadinessWorkbench.tsx',
)

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 数据生产中心注册"盘后就绪"tab =====
test('数据生产中心注册"盘后就绪"tab 并渲染工作台', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes("'readiness'"), 'tab 类型必须包含 readiness')
  assert.ok(src.includes('盘后就绪'), 'tab 标签必须为"盘后就绪"')
  assert.ok(src.includes('AdminReadinessWorkbench'), '必须渲染 AdminReadinessWorkbench')
  assert.ok(src.includes("activeTab === 'readiness'"), '必须按 tab 条件渲染')
})

// ===== 2. 工作台消费后端正式发布读模型 =====
test('工作台消费 GET /v1/admin/readiness/{trade_date}', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(src.includes('useAdminProductReadiness'), '必须调用 useAdminProductReadiness')
  assert.ok(src.includes('readinessQuery'), '必须读取就绪查询结果')
})

// ===== 3. 展示九节点状态 =====
test('展示九节点状态（九个产品名）', () => {
  const src = readSource(WORKBENCH_PATH)
  const products = [
    'daily_facts',
    'board_facts',
    'stock_core',
    'dsa_projection',
    'chip',
    'state_events',
    'auction_anchor',
    'board_aggregation',
    'review',
  ]
  for (const p of products) {
    assert.ok(src.includes(p), `必须覆盖产品节点 ${p}`)
  }
})

// ===== 4. 展示闭包状态与 freshness 标志 =====
test('展示闭包状态 + mandatory freshness 标志', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(src.includes('data.closure'), '必须展示闭包状态')
  assert.ok(src.includes('mandatoryProductsReady'), '必须展示核心链就绪')
  assert.ok(src.includes('mandatoryProductsFullyFresh'), '必须展示完全新鲜')
  assert.ok(src.includes('enhancementJobsTerminal'), '必须展示增强任务终态')
})

// ===== 4b. [Phase 4] 六态闭包：mandatory_ready_enhancing 必须有明确文案 =====
test('六态闭包：mandatory_ready_enhancing 有明确文案', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(
    src.includes('mandatory_ready_enhancing'),
    '必须覆盖 mandatory_ready_enhancing 闭包态',
  )
  assert.ok(
    src.includes('核心就绪·增强推进中'),
    'mandatory_ready_enhancing 必须有中文文案',
  )
})

// ===== 5. 展示治理报告 =====
test('展示治理报告（lineage/stale/unmatched/degraded）', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(src.includes('pointerLineage'), '必须展示数据来源 lineage')
  assert.ok(src.includes('staleChildren'), '必须展示陈旧子产品')
  assert.ok(src.includes('unmatchedActiveChildren'), '必须展示未匹配运行中增强')
  assert.ok(src.includes('degradedReasons'), '必须展示降级原因')
})

// ===== 6. loading / error / empty 状态 =====
test('loading/error/empty 状态均有明确处理', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(src.includes('isLoading'), '必须处理 loading')
  assert.ok(src.includes('isError'), '必须处理 error')
  assert.ok(src.includes('暂无就绪数据'), '必须处理 empty')
})

// ===== 7. 用户侧只消费正式 publication（只读，不触发写操作）=====
test('本页为只读诊断，不触发任何写操作', () => {
  const src = readSource(WORKBENCH_PATH)
  assert.ok(!src.includes('useMutation'), '工作台不得触发写变更')
  assert.ok(!src.includes('apiClient.post'), '工作台不得调用写接口')
})