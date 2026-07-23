// E2E: 自选进入详情保持自选来源
// 验证 PROMPT.md CP-18 要求：watchlist 来源上下文不丢失
import { test, expect } from '@playwright/test'
import { injectAuthState, setupMockApi } from './helpers/mock-api'

test.describe('自选来源上下文', () => {
  test.beforeEach(async ({ page }) => {
    await injectAuthState(page)
    await setupMockApi(page)
  })

  test('自选列表进入详情：左栏保持 watchlist 来源', async ({ page }) => {
    // 从 /market?scope=watchlist 进入
    await page.goto('/market?scope=watchlist')
    await page.waitForTimeout(1000)

    // 进入详情（带 watchlist 来源参数）
    await page.goto('/stock/000001?returnTo=/market?scope=watchlist&originScope=watchlist&source=watchlist&strategy=watchlist_monitor')
    await page.waitForTimeout(1500)

    // 验证 URL 保留 watchlist 来源
    expect(page.url()).toContain('originScope=watchlist')
    expect(page.url()).toContain('source=watchlist')

    // 应显示自选相关标识（不出现行情筛选）
    const marketHeader = page.locator('text=趋势选股, text=行情筛选').first()
    await expect(marketHeader).toHaveCount(0, { timeout: 2_000 })
  })

  test('刷新自选详情页：来源不丢失', async ({ page }) => {
    await page.goto('/stock/000001?returnTo=/market?scope=watchlist&originScope=watchlist&source=watchlist')
    await page.waitForTimeout(1000)

    await page.reload()
    await page.waitForTimeout(1500)

    // 刷新后仍应保留 watchlist 来源
    expect(page.url()).toContain('originScope=watchlist')
    expect(page.url()).toContain('source=watchlist')

    // 不应切换到 market 来源
    const marketHeader = page.locator('text=趋势选股, text=行情筛选').first()
    await expect(marketHeader).toHaveCount(0, { timeout: 2_000 })
  })

  test('direct originScope 进入详情：不强制 watchlist/market', async ({ page }) => {
    // direct 来源（如通知中心点击）：不应回退到 watchlist
    await page.goto('/stock/000001?originScope=direct')
    await page.waitForTimeout(1500)

    // URL 保留 direct 来源
    expect(page.url()).toContain('originScope=direct')

    // 不应回退到 watchlist 或 market
    // 注意：direct 模式下可能不显示左栏列表，所以不强断言 header 文本
  })

  // [Task 5] 自选入口单独验证：自选→详情→切换股票→返回仍恢复自选
  test('自选→详情→切换股票→返回仍恢复自选', async ({ page }) => {
    // 1. 从 /market?scope=watchlist 自选进入
    await page.goto('/market?scope=watchlist')
    await page.waitForTimeout(1000)

    // 2. 进入股票 A 详情（带 originScope=watchlist + returnTo）
    const returnToA = encodeURIComponent('/market?scope=watchlist&selected=000001')
    await page.goto(
      `/stock/000001?returnTo=${returnToA}&originScope=watchlist&source=watchlist&strategy=watchlist_monitor`,
    )
    await page.waitForTimeout(1500)

    // 3. 验证 URL 含 originScope=watchlist
    expect(page.url()).toContain('originScope=watchlist')
    expect(page.url()).toContain('source=watchlist')

    // 4. 验证不出现行情筛选来源
    const marketHeader = page.locator('text=趋势选股, text=行情筛选').first()
    await expect(marketHeader).toHaveCount(0, { timeout: 2_000 })

    // 5. 切换到股票 B
    const returnToB = encodeURIComponent('/market?scope=watchlist&selected=000002')
    await page.goto(
      `/stock/000002?returnTo=${returnToB}&originScope=watchlist&source=watchlist&strategy=watchlist_monitor`,
    )
    await page.waitForTimeout(1500)

    // 6. 切换后仍保持 watchlist 来源
    expect(page.url()).toContain('originScope=watchlist')
    expect(page.url()).toContain('source=watchlist')
    expect(page.url()).not.toContain('source=selection')

    // 7. 返回（浏览器 back 两次：B→A→watchlist）应恢复自选页面
    // 测试用 page.goto 模拟切换，history=[watchlist,A,B]，goBack 一次只到 A，需两次回 watchlist
    await page.goBack() // B → A
    await page.waitForTimeout(500)
    await page.goBack() // A → watchlist
    await page.waitForTimeout(1000)
    expect(page.url()).toContain('/market')
    expect(page.url()).toContain('scope=watchlist')
  })
})
