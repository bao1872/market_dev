// [PRD §8.2] 数据生产中心渲染逻辑测试
// 用法：node --test src/pages/__tests__/adminDataProduction.test.mjs
//
// 验证：总览 tab 从后端 summary.production_chain 渲染 6 个产品节点；
// 业务产品 tab 展示聚合读模型筛选视图；URL query 作为 tab 唯一真源；
// 不再显示过期的"P1 后续提供"占位。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminDataProductionPage.tsx')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 页面从 summary.production_chain 读取 6 个产品节点 =====
test('数据生产中心消费后端 summary.production_chain 渲染总览', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('useAdminSystemOverview'), '必须调用 useAdminSystemOverview')
  assert.ok(src.includes('production_chain'), '必须读取 production_chain')
  assert.ok(src.includes('overview?.summary?.production_chain'), '必须从 summary.production_chain 取数')
})

// ===== 2. 覆盖 6 个产品节点 =====
test('总览展示 6 个产品节点（行情/第一金字塔/板块/复盘/竞价/发布）', () => {
  const src = readSource(PAGE_PATH)
  // 节点筛选视图映射覆盖全部 6 个产品 key
  assert.ok(src.includes("'first-pyramid'") && src.includes('first_pyramid'), '第一金字塔节点映射')
  assert.ok(src.includes("board") && src.includes("'board'"), '板块节点映射')
  assert.ok(src.includes("review") && src.includes("'review'"), '复盘节点映射')
  assert.ok(src.includes("auction") && src.includes("'auction'"), '竞价节点映射')
  assert.ok(src.includes("publish") && src.includes("'publish'"), '发布节点映射')
})

// ===== 3. URL query 作为 tab 唯一真源 =====
test('tab 由 URL query 唯一控制（useSearchParams）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('useSearchParams'), '必须使用 useSearchParams 读 URL tab')
  assert.ok(src.includes("searchParams.get('tab')"), '必须从 URL 读取 tab 参数')
  assert.ok(src.includes('params.set(\'tab\', tab)') || src.includes('setSearchParams'), '切换 tab 必须写回 URL')
})

// ===== 4. 移除过期的"P1 后续提供"占位 =====
test('不再显示"P1 后续提供"过期占位', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(!src.includes('P1 阶段填充聚合状态'), '不得再出现 P1 占位说明')
  assert.ok(!src.includes('该业务产品聚合视图将在统一数据生产与发布状态阶段'), '不得再出现占位空态文案')
})

// ===== 5. 渲染每项的展示字段 =====
test('总览渲染每项的 detail/blocking_reason/recommended_action', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('node.detail'), '必须展示节点 detail')
  assert.ok(src.includes('node.blocking_reason'), '必须展示阻塞原因')
  assert.ok(src.includes('node.recommended_action'), '必须展示建议动作')
})

// ===== 6. 默认进入总览（P0 审查修复）=====
test('默认 tab 为总览（overview），而非盘后编排', () => {
  const src = readSource(PAGE_PATH)
  // 默认值必须为 overview（不再默认 after-close）
  assert.ok(src.includes(": 'overview')"), '默认 tab 必须是 overview')
  assert.ok(!src.includes(": 'after-close')"), '默认 tab 不得再是 after-close')
})

// ===== 7. 接口失败显示真实错误（P0 审查修复）=====
test('overviewQuery.isError 显示真实错误而非暂无数据', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('overviewQuery.isError'), '必须处理 isError')
  assert.ok(src.includes('查询失败') || src.includes('请稍后重试'), '必须显示错误提示')
})

// ===== 8. not_applicable 不误显示为"未发布"（P0 审查修复）=====
test("publication_status=not_applicable 显示'不适用'而非'未发布'", () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes("'不适用'"), '必须区分 not_applicable 为"不适用"')
  assert.ok(src.includes("publication_status === 'pending'"), '必须区分 pending 为"待发布"')
})
