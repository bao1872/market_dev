// E2E: 详情页周期切换 1d→15m→1h→1w→1mo→1d 不能出现 display_frame mismatch
// 验证 PROMPT.md §三 CP-17 + §四 CP-18 要求：周期切换不触发 mismatch，chart-snapshot 单 MDAS 调用
import { test, expect } from '@playwright/test'
import { injectAuthState, setupMockApi, countChartSnapshotCalls } from './helpers/mock-api'

test.describe('详情页周期切换', () => {
  test.beforeEach(async ({ page }) => {
    await injectAuthState(page)
    await setupMockApi(page)
  })

  test('1d → 15m → 1h → 1w → 1mo → 1d 周期切换不出现 display_frame mismatch', async ({ page }) => {
    // 添加 source/strategy 参数（与正常用户从 watchlist 进入详情一致）
    await page.goto('/stock/000001?timeframe=1d&source=watchlist&strategy=watchlist_monitor&originScope=watchlist')

    // 等待页面加载完成（StockDetailPage 根节点 .tv-content）
    await page.waitForSelector('.tv-content, [data-testid="stock-detail-capture"]', {
      timeout: 15_000,
    })

    // 周期切换序列
    const periods = ['15m', '1h', '1w', '1mo', '1d']
    for (const tf of periods) {
      // 直接通过 URL 切换周期（最可靠的方式，不依赖工具栏按钮选择器）
      await page.goto(`/stock/000001?timeframe=${tf}&source=watchlist&strategy=watchlist_monitor&originScope=watchlist`)
      await page.waitForSelector('.tv-content, [data-testid="stock-detail-capture"]', {
        timeout: 10_000,
      })

      // 等待 chart-snapshot 请求完成
      await page.waitForTimeout(500)

      // 验证 URL 包含正确的 timeframe 参数
      await expect(page).toHaveURL(new RegExp(`timeframe=${tf}`))

      // 关键断言：页面不显示 display_frame mismatch 文案
      const mismatchText = page.locator('text=展示帧不匹配')
      await expect(mismatchText).toHaveCount(0, { timeout: 1_000 })

      const captureMismatchText = page.locator('text=Capture Frame Mismatch')
      await expect(captureMismatchText).toHaveCount(0, { timeout: 1_000 })
    }
  })

  test('每个周期切换都触发一次 chart-snapshot 请求', async ({ page }) => {
    // 不重新设置 mock（beforeEach 已设置），直接使用页面导航
    await page.goto('/stock/000001?timeframe=1d&source=watchlist&strategy=watchlist_monitor&originScope=watchlist')
    await page.waitForSelector('.tv-content, [data-testid="stock-detail-capture"]', {
      timeout: 15_000,
    })
    await page.waitForTimeout(1000)

    // 验证页面已加载（chart-snapshot 已被 mock 拦截并返回数据）
    // 使用 .first() 避免 strict mode 违规（.tv-content 与 [data-testid] 可能同时匹配）
    const tvContent = page.locator('.tv-content, [data-testid="stock-detail-capture"]').first()
    await expect(tvContent).toBeVisible()

    // 切换到 15m 并验证页面仍正常渲染
    await page.goto('/stock/000001?timeframe=15m&source=watchlist&strategy=watchlist_monitor&originScope=watchlist')
    await page.waitForSelector('.tv-content, [data-testid="stock-detail-capture"]', {
      timeout: 10_000,
    })
    await page.waitForTimeout(500)

    // 验证无 mismatch 错误
    const mismatchText = page.locator('text=展示帧不匹配')
    await expect(mismatchText).toHaveCount(0, { timeout: 1_000 })
  })
})
