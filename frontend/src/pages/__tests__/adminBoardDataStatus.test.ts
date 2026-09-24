// [BOARD-LOCAL-OWNERSHIP-01] 板块/概念「本地手动同步」+ 盘后步骤词表收口契约测试。
// 用法：node --experimental-strip-types --test src/pages/__tests__/adminBoardDataStatus.test.ts
//
// 覆盖：
// - 盘后 current 步骤序列不含 syncing_boards；legacy 事件仍可安全展示
// - AdminDataProductionPage「板块」Tab 为独立本地手动状态视图（不从 production_chain 取数）
// - 展示只读、无「立即同步」服务端动作、无基于 age 的过期/超时判定
// - 计数与最后成功时间渲染；最近一次失败但数据仍在 → 仍可用 + 提示

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import type { AdminBoardSyncStatusResponse } from '../../api/admin'
import {
  DEFAULT_STEP_ORDER,
  STEP_LABELS,
  stepLabel,
} from '../adminAfterClosePipelineHelpers.ts'
import {
  buildBoardSyncDisplay,
  formatBoardSyncTime,
  BOARD_SYNC_STALE_REUSE_NOTICE,
  BOARD_SYNC_HELP_TEXT,
} from '../adminBoardDataStatusHelpers.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PAGE_PATH = join(__dirname, '..', 'AdminDataProductionPage.tsx')
const BOARD_COMPONENT_PATH = join(__dirname, '..', 'AdminBoardDataStatus.tsx')
const ADMIN_API_PATH = join(__dirname, '..', '..', 'api', 'admin.ts')
const AFTER_CLOSE_API_PATH = join(__dirname, '..', '..', 'api', 'adminAfterClose.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

function makeStatus(
  over: Partial<AdminBoardSyncStatusResponse> = {},
): AdminBoardSyncStatusResponse {
  return {
    mode: 'local_manual',
    source: 'wencai',
    available: true,
    last_success_at: '2026-09-20T07:30:00+00:00',
    board_count: 767,
    industry_count: 257,
    concept_count: 510,
    membership_count: 191750,
    stock_count: 5400,
    recent_attempt: null,
    ...over,
  }
}

// ============================================================
// 1. 盘后 current 步骤词表
// ============================================================

test('1a. DEFAULT_STEP_ORDER 不含 syncing_boards（已迁出盘后 DAG）', () => {
  assert.ok(!DEFAULT_STEP_ORDER.includes('syncing_boards'))
  assert.ok(DEFAULT_STEP_ORDER.includes('refreshing_daily'))
  assert.ok(DEFAULT_STEP_ORDER.includes('checking_coverage'))
  assert.ok(DEFAULT_STEP_ORDER.includes('computing_history'))
})

test('1b. legacy syncing_boards 事件仍可安全展示（标签 + 未知降级）', () => {
  // 历史 run 若返回 syncing_boards，前端必须能展示而非崩溃
  assert.strictEqual(stepLabel('syncing_boards'), '同步板块（历史）')
  assert.ok(STEP_LABELS['syncing_boards'])
  // 任意未知 step 字符串安全降级为 key 本身
  assert.strictEqual(stepLabel('some_unknown_legacy_step'), 'some_unknown_legacy_step')
})

test('1c. AfterCloseRestartStep 词表不含 syncing_boards', () => {
  const src = readSource(AFTER_CLOSE_API_PATH)
  const block = src.slice(src.indexOf('export type AfterCloseRestartStep'))
  const unionBody = block.slice(0, block.indexOf('\n\n'))
  assert.ok(!unionBody.includes('syncing_boards'), 'restart 词表不得含 syncing_boards')
})

// ============================================================
// 2. 板块 Tab 为独立本地手动状态视图
// ============================================================

test('2a. 板块 Tab 存在且渲染独立组件（不从 production_chain 取数）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes("key: 'board'"), '板块 Tab 必须存在')
  assert.ok(src.includes('AdminBoardDataStatus'), '板块 Tab 必须渲染独立组件')
  assert.ok(
    src.includes("activeTab === 'board'"),
    '板块 Tab 必须走独立渲染分支',
  )
  assert.ok(
    !/TAB_TO_CHAIN_KEY[\s\S]*?board:\s*'board'/.test(src),
    '板块不得再映射到 production_chain 的 board 节点',
  )
})

test('2b. 板块视图使用专用 board status API（非 after-close）', () => {
  const component = readSource(BOARD_COMPONENT_PATH)
  assert.ok(
    component.includes('useAdminBoardSyncStatus'),
    '组件必须使用专用 useAdminBoardSyncStatus hook',
  )
  const api = readSource(ADMIN_API_PATH)
  assert.ok(
    api.includes("'/v1/admin/board-sync/status'"),
    'admin API 必须调用 /v1/admin/board-sync/status',
  )
})

test('2c. 板块视图声明本地手动更新与本地命令提示', () => {
  const component = readSource(BOARD_COMPONENT_PATH)
  const helper = readSource(join(__dirname, '..', 'adminBoardDataStatusHelpers.ts'))
  assert.ok(
    component.includes('本地手动同步') || helper.includes('本地手动同步'),
    '必须声明更新方式：本地手动同步',
  )
  assert.ok(
    component.includes('panji-board-sync') || helper.includes(BOARD_SYNC_HELP_TEXT),
    '必须提示本地命令 scripts/ops/panji-board-sync',
  )
  assert.ok(
    helper.includes('问财'),
    '必须声明数据源：问财',
  )
})

test('2e. 板块 Tab 不得渲染通用「数据产品」筛选卡（RC3）', () => {
  const src = readSource(PAGE_PATH)
  // 通用卡必须由**正向枚举**控制，而不是不断叠加 activeTab !== ... 排除项
  assert.ok(
    src.includes('BUSINESS_PRODUCT_TABS'),
    '通用产品卡必须由正向枚举 BUSINESS_PRODUCT_TABS 控制',
  )
  assert.ok(
    !/activeTab !== 'after-close'[\s\S]{0,120}activeTab !== 'readiness'/.test(src),
    '不得再由 activeTab !== ... 排除串控制通用卡（board 会漏进去）',
  )
  const blockStart = src.indexOf('BUSINESS_PRODUCT_TABS: readonly')
  assert.ok(blockStart > 0, '必须存在 BUSINESS_PRODUCT_TABS 定义')
  const block = src.slice(blockStart, src.indexOf('] as const', blockStart))
  assert.ok(!block.includes("'board'"), 'board 不得出现在 BUSINESS_PRODUCT_TABS 中')
  // 通用卡的渲染条件必须引用该枚举
  assert.ok(
    src.includes('BUSINESS_PRODUCT_TABS.includes(activeTab)'),
    '通用卡渲染条件必须为 BUSINESS_PRODUCT_TABS.includes(activeTab)',
  )
})

test('2d. 无服务端「立即同步」动作，且无基于 age 的过期判定', () => {
  const component = readSource(BOARD_COMPONENT_PATH)
  for (const forbidden of ['立即同步', '现在同步', '同步问财']) {
    assert.ok(!component.includes(forbidden), `不得包含服务端同步动作文案: ${forbidden}`)
  }
  for (const forbidden of ['过期', '超时未更新', '必须今日更新', '天未更新']) {
    assert.ok(!component.includes(forbidden), `不得包含基于 age 的判定文案: ${forbidden}`)
  }
  // 只读：组件不得触发任何 mutation
  assert.ok(!component.includes('useMutation'), '板块状态视图必须为只读（无 mutation）')
})

// ============================================================
// 3. 展示模型（纯函数）
// ============================================================

test('3a. 计数与最后成功时间渲染', () => {
  const view = buildBoardSyncDisplay(makeStatus())
  assert.strictEqual(view.updateMode, '本地手动同步')
  assert.strictEqual(view.source, '问财')
  assert.strictEqual(view.boardCount, 767)
  assert.strictEqual(view.industryCount, 257)
  assert.strictEqual(view.conceptCount, 510)
  assert.strictEqual(view.membershipCount, 191750)
  assert.strictEqual(view.stockCount, 5400)
  assert.ok(view.lastSuccessText !== '暂无', '有 last_success_at 时必须渲染时间')
  assert.strictEqual(view.dataNotice, null)
})

test('3b. 无 last_success_at 时显示「暂无」', () => {
  const view = buildBoardSyncDisplay(
    makeStatus({ available: false, last_success_at: null }),
  )
  assert.strictEqual(view.available, false)
  assert.strictEqual(view.lastSuccessText, '暂无')
})

test('3c. 最近一次失败但数据仍有效 → 仍可用 + 复用提示', () => {
  const view = buildBoardSyncDisplay(
    makeStatus({
      available: true,
      recent_attempt: {
        status: 'failed',
        source: 'wencai',
        mode: 'local_manual',
        error_code: 'StagingValidationError',
        completed_at: '2026-09-21T02:00:00+00:00',
      },
    }),
  )
  assert.strictEqual(view.available, true)
  assert.ok(view.recentAttempt)
  assert.strictEqual(view.recentAttempt?.isFailed, true)
  assert.strictEqual(view.recentAttempt?.errorCode, 'StagingValidationError')
  assert.strictEqual(view.dataNotice, BOARD_SYNC_STALE_REUSE_NOTICE)
})

test('3d. 成功尝试不产生复用提示', () => {
  const view = buildBoardSyncDisplay(
    makeStatus({
      recent_attempt: { status: 'succeeded', completed_at: '2026-09-21T02:00:00+00:00' },
    }),
  )
  assert.strictEqual(view.recentAttempt?.isFailed, false)
  assert.strictEqual(view.dataNotice, null)
})

test('3e. 展示模型不含任何 stale/overdue/frequency 分类字段', () => {
  const view = buildBoardSyncDisplay(makeStatus()) as unknown as Record<string, unknown>
  for (const forbidden of ['is_stale', 'stale_after_days', 'next_due_at', 'overdue', 'required_frequency']) {
    assert.ok(!(forbidden in view), `展示模型不得含 ${forbidden}`)
  }
})

test('3f. formatBoardSyncTime 对 null/非法值安全', () => {
  assert.strictEqual(formatBoardSyncTime(null), '暂无')
  assert.strictEqual(formatBoardSyncTime(undefined), '暂无')
  assert.strictEqual(formatBoardSyncTime('not-a-date'), 'not-a-date')
})
