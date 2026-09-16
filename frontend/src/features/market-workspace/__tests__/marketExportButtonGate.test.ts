// [S2-A-C1] 导出按钮 policy 契约测试（纯函数，无 React 依赖）
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/marketExportButtonGate.test.ts
//
// 固定 UI policy owner（resolveMarketExportPolicy）行为：
//   C1a-1  非 admin（无论 accessReady）不渲染导出按钮、不挂载 onExport
//   C1a-2  admin 但 accessReady=false（capability 未解析完成）不暴露按钮
//   C1a-3  admin + accessReady + 非 in-flight ⇒ 按钮可见、可点击、onExport 已挂载
//   C1a-4  admin + accessReady + exporting（in-flight）⇒ 按钮可见但禁用
//   C1a-5  exporting 只影响按钮可用态，不改变 admin 可见性
//
// 后端 require_admin 才是最终安全边界；此 policy 仅做 UX 收口与防双击。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { resolveMarketExportPolicy } from '../marketExportGate.ts'

// ===== C1a-1 非 admin 无入口 =====

test('C1a-1: 非 admin（无论 accessReady）不渲染导出按钮、不挂载 onExport', () => {
  for (const accessReady of [true, false]) {
    const p = resolveMarketExportPolicy({ isAdmin: false, accessReady, exporting: false })
    assert.equal(p.canShowExportButton, false, '非 admin 不得出现导出入口')
    assert.equal(
      p.onExportWired,
      false,
      '非 admin 不得持有 onExport（后端 require_admin 才是最终安全边界）',
    )
    assert.equal(p.exportButtonEnabled, false)
  }
})

// ===== C1a-2 capability 未就绪不提前暴露 =====

test('C1a-2: admin 但 accessReady=false（capability 未解析完成）不暴露按钮', () => {
  const p = resolveMarketExportPolicy({ isAdmin: true, accessReady: false, exporting: false })
  assert.equal(p.canShowExportButton, false, 'capability 未就绪时按钮不得提前暴露')
  assert.equal(p.onExportWired, false)
  assert.equal(p.exportButtonEnabled, false)
})

// ===== C1a-3 正常 admin 可导出 =====

test('C1a-3: admin + accessReady + 非 in-flight ⇒ 按钮可见、可点击、onExport 已挂载', () => {
  const p = resolveMarketExportPolicy({ isAdmin: true, accessReady: true, exporting: false })
  assert.equal(p.canShowExportButton, true)
  assert.equal(p.onExportWired, true)
  assert.equal(p.exportButtonEnabled, true, '空闲时按钮必须可点击')
})

// ===== C1a-4 in-flight 禁用但不消失 =====

test('C1a-4: admin + accessReady + exporting（in-flight）⇒ 按钮可见但禁用', () => {
  const p = resolveMarketExportPolicy({ isAdmin: true, accessReady: true, exporting: true })
  assert.equal(p.canShowExportButton, true, '导出中按钮不得消失（否则用户以为没触发）')
  assert.equal(
    p.onExportWired,
    true,
    'onExport 仍由 admin 决定是否挂载（in-flight 拦截在 handler 自身守卫内）',
  )
  assert.equal(p.exportButtonEnabled, false, 'in-flight 必须禁用按钮防双击/多 tab/并发')
})

// ===== C1a-5 exporting 只影响可用态 =====

test('C1a-5: exporting 只影响按钮可用态，不改变 admin 可见性', () => {
  const idle = resolveMarketExportPolicy({ isAdmin: true, accessReady: true, exporting: false })
  const busy = resolveMarketExportPolicy({ isAdmin: true, accessReady: true, exporting: true })
  assert.equal(idle.canShowExportButton, busy.canShowExportButton)
  assert.equal(idle.onExportWired, busy.onExportWired)
  assert.notEqual(idle.exportButtonEnabled, busy.exportButtonEnabled, 'in-flight 应禁用按钮')
})
