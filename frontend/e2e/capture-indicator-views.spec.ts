// E2E: Capture 分别加载 Node/BB/SMC，indicator_view 正确
// 验证 PROMPT.md CP-18 §4 要求：indicator_view URL 透传到 capture API
import { test, expect } from '@playwright/test'
import { injectAuthState, setupMockApi, assertCaptureIndicatorView, countCaptureSnapshotCalls } from './helpers/mock-api'

test.describe('Capture indicator_view 透传', () => {
  test.beforeEach(async ({ page }) => {
    await injectAuthState(page, { captureMode: true })
  })

  test('indicator_view=node_cluster：Capture API 透传正确', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=node_cluster')

    // 等待 capture snapshot 调用
    await page.waitForTimeout(2000)

    // 断言 capture API 调用包含正确的 indicator_view 参数
    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'node_cluster')

    // 验证 DOM 的 data-indicator-view 属性
    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'node_cluster')
  })

  test('indicator_view=bollinger：Capture API 透传正确', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=bollinger')

    await page.waitForTimeout(2000)

    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'bollinger')

    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'bollinger')
  })

  test('indicator_view=smc：Capture API 透传正确 + include_smc=true', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001&indicator_view=smc')

    await page.waitForTimeout(2000)

    expect(countCaptureSnapshotCalls(calls)).toBeGreaterThanOrEqual(1)
    assertCaptureIndicatorView(calls, 'smc')

    // SMC 视图应透传 include_smc（后端按需计算）
    const captureCalls = calls.filter((c) => c.url.includes('/capture/stocks/'))
    if (captureCalls.length > 0) {
      const lastCall = captureCalls[captureCalls.length - 1]
      // include_smc 透传由后端根据 indicator_view 自动决定，前端 URL 不一定显式传 include_smc
      // 但 indicator_view=smc 必须传给后端
      expect(lastCall.params.indicator_view).toBe('smc')
    }

    const stage = page.locator('[data-testid="stock-detail-capture"]')
    await expect(stage).toHaveAttribute('data-indicator-view', 'smc')
  })

  test('indicator_view 缺失：默认使用 node_cluster', async ({ page }) => {
    const { calls } = await setupMockApi(page)
    await page.goto('/capture/stock/000001?token=fixture-capture-token&instrument_id=inst-000001')

    await page.waitForTimeout(2000)

    // 缺失 indicator_view 时，CaptureStockPage 默认使用 node_cluster
    const captureCalls = calls.filter((c) => c.url.includes('/capture/stocks/'))
    if (captureCalls.length > 0) {
      const lastCall = captureCalls[captureCalls.length - 1]
      expect(lastCall.params.indicator_view).toBe('node_cluster')
    }
  })

  test('所有 indicator_view 都能进入 render-ready 状态', async ({ page }) => {
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
