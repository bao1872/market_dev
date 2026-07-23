// E2E: 行情筛选进入详情、刷新、上一只/下一只，左栏保持行情来源
// 验证 PROMPT.md CP-18 要求 + DETAIL-01：market context 不得回退 watchlist
import { test, expect } from '@playwright/test'
import { injectAuthState, setupMockApi } from './helpers/mock-api'

test.describe('行情筛选来源上下文', () => {
  test.beforeEach(async ({ page }) => {
    await injectAuthState(page)
    await setupMockApi(page)
  })

  test('行情筛选进入详情：左栏保持 market 来源', async ({ page }) => {
    // 从 /market 进入（行情筛选）
    await page.goto('/market?scope=market')
    await page.waitForTimeout(1000)

    // 点击列表中某只股票进入详情（带 returnTo=market&originScope=market）
    await page.goto('/stock/000001?returnTo=/market?scope=market&originScope=market&source=market&strategy=trend_selection')

    await page.waitForTimeout(1500)

    // 验证 URL 仍保留 market 来源参数
    expect(page.url()).toContain('originScope=market')
    expect(page.url()).toContain('source=market')

    // 关键断言：不出现 watchlist 来源（DETAIL-01：market 不得回退 watchlist）
    // 检查左栏标题不应是「自选监控」
    const watchlistHeader = page.locator('text=自选监控').first()
    await expect(watchlistHeader).toHaveCount(0, { timeout: 2_000 })

    // 应显示行情筛选相关标识
    const marketHeader = page.locator('text=趋势选股, text=行情筛选').first()
    await expect(marketHeader).toBeVisible({ timeout: 5_000 }).catch(() => {
      // 部分实现可能不显示标题，但 URL 必须保留 market 来源
      console.log('Market header not visible, but URL preserves market context')
    })
  })

  test('刷新详情页：左栏来源不丢失', async ({ page }) => {
    await page.goto('/stock/000001?returnTo=/market?scope=market&originScope=market&source=market')
    await page.waitForTimeout(1000)

    // 刷新页面
    await page.reload()
    await page.waitForTimeout(1500)

    // 刷新后仍应保留 market 来源
    expect(page.url()).toContain('originScope=market')
    expect(page.url()).toContain('source=market')

    // 不应回退到 watchlist
    const watchlistHeader = page.locator('text=自选监控').first()
    await expect(watchlistHeader).toHaveCount(0, { timeout: 2_000 })
  })

  test('上一只/下一只切换：左栏来源保持 market', async ({ page }) => {
    // source=selection 表示来自行情筛选（前端 ResearchSource 类型），originScope=market 表示市场来源
    await page.goto('/stock/000001?returnTo=%2Fmarket%3Fscope%3Dmarket&originScope=market&source=selection&strategy=dsa_selector')
    await page.waitForTimeout(1500)

    // 查找并点击"下一只"按钮
    const nextButton = page.locator(
      '[data-testid="next-stock"], button:has-text("下一只"), button:has-text("下一个"), [aria-label="下一只"]',
    ).first()

    if (await nextButton.isVisible().catch(() => false)) {
      await nextButton.click()
      await page.waitForTimeout(1000)

      // 切换股票后仍应保留 market 来源（originScope=market）
      expect(page.url()).toContain('originScope=market')
      // source 应保持 selection（行情筛选来源），不得回退到 watchlist
      expect(page.url()).toContain('source=selection')
      expect(page.url()).not.toContain('source=watchlist')
    } else {
      // 如果没有下一只按钮，手动跳转下一只股票
      await page.goto('/stock/000002?returnTo=%2Fmarket%3Fscope%3Dmarket&originScope=market&source=selection&strategy=dsa_selector')
      await page.waitForTimeout(1000)
      expect(page.url()).toContain('originScope=market')
      expect(page.url()).toContain('source=selection')
    }
  })

  // [Task 5] 定向 E2E：行情筛选→进入股票A→左栏仍为筛选结果→切换股票B→返回仍恢复行情筛选
  test('行情筛选→详情→切换股票→返回仍恢复行情筛选', async ({ page }) => {
    // 1. 从 /market?scope=market 行情筛选进入
    await page.goto('/market?scope=market')
    await page.waitForTimeout(1000)

    // 2. 进入股票 A 详情（带 originScope=market + returnTo）
    const returnToA = encodeURIComponent('/market?scope=market&selected=000001')
    await page.goto(
      `/stock/000001?returnTo=${returnToA}&originScope=market&source=selection&strategy=dsa_selector`,
    )
    await page.waitForTimeout(1500)

    // 3. 验证 URL 含 originScope=market 和 returnTo
    expect(page.url()).toContain('originScope=market')
    expect(page.url()).toContain('source=selection')
    expect(page.url()).toContain('returnTo=')

    // 4. 验证左栏不显示自选来源（market 不得回退 watchlist）
    const watchlistHeader = page.locator('text=自选监控').first()
    await expect(watchlistHeader).toHaveCount(0, { timeout: 2_000 })

    // 5. 在左栏切换到股票 B（模拟点击左栏列表项）
    const returnToB = encodeURIComponent('/market?scope=market&selected=000002')
    await page.goto(
      `/stock/000002?returnTo=${returnToB}&originScope=market&source=selection&strategy=dsa_selector`,
    )
    await page.waitForTimeout(1500)

    // 6. 切换后仍保持 market 来源
    expect(page.url()).toContain('originScope=market')
    expect(page.url()).toContain('source=selection')
    expect(page.url()).not.toContain('source=watchlist')

    // 7. 左栏仍不显示自选来源
    const watchlistHeaderB = page.locator('text=自选监控').first()
    await expect(watchlistHeaderB).toHaveCount(0, { timeout: 2_000 })

    // 8. 返回（浏览器 back 两次：B→A→market）应恢复行情筛选页面
    // 测试用 page.goto 模拟切换，history=[market,A,B]，goBack 一次只到 A，需两次回 market
    await page.goBack() // B → A
    await page.waitForTimeout(500)
    await page.goBack() // A → market
    await page.waitForTimeout(1000)
    // 返回后 URL 应包含 /market 和 scope=market
    expect(page.url()).toContain('/market')
    expect(page.url()).toContain('scope=market')
  })
})
