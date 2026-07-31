// E2E: Capture 固定组合视图（CHANGE-20260728-010）
// 验证：前端固定发送 indicator_view=structure_node，不再透传 URL 参数
// 旧 URL 携带的 node_cluster/bollinger/smc 仅作历史兼容，不影响 API 请求和渲染
import { test, expect } from '@playwright/test'
import { injectAuthState, setupMockApi, assertCaptureIndicatorView, countCaptureSnapshotCalls } from './helpers/mock-api'

test.describe('Capture indicator_view 固定组合视图', () => {
  test.beforeEach(async ({ page }) => {
    await injectAuthState(page, { captureMode: true })
  })

  test('URL indicator_view=node_cluster：API 仍发送 structure_node', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=node_cluster')

    // 等待 capture snapshot 调用
    await page.waitForTimeout(2000)

    // [CHANGE-20260728-010] 前端固定发送 structure_node，不透传 URL 参数
    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'structure_node')

    // data-indicator-view 固定为 structure_node
    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'structure_node')
  })

  test('URL indicator_view=bollinger：API 仍发送 structure_node', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=bollinger')

    await page.waitForTimeout(2000)

    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'structure_node')

    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'structure_node')
  })

  test('URL indicator_view=smc：API 仍发送 structure_node', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=smc')

    await page.waitForTimeout(2000)

    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'structure_node')

    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'structure_node')
  })

  test('URL indicator_view 缺失：API 发送 structure_node', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001')

    await page.waitForTimeout(2000)

    // [CHANGE-20260728-010] 缺失 URL 参数时，前端仍固定发送 structure_node
    const captureCalls = calls.filter((c) => c.url.includes('/capture/stocks/'))
    if (captureCalls.length > 0) {
      const lastCall = captureCalls[captureCalls.length - 1]
      expect(lastCall.params.indicator_view).toBe('structure_node')
    }
  })

  test('所有 indicator_view URL 都能进入 render-ready 状态', async ({ page }) => {
    const views = ['node_cluster', 'bollinger', 'smc'] as const
    for (const view of views) {
      await page.unroute('**/api/**')
      await setupMockApi(page)
      await page.goto(`/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=${view}`)

      // 等待 data-render-ready="true"（最长 15s）
      await page.waitForSelector('[data-render-ready="true"]', { timeout: 15_000 })
    }
  })
})
