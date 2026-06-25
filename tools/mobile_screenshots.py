"""移动端多尺寸截图脚本 - 用于验证响应式布局。

用法:
    python tools/mobile_screenshots.py

环境:
    需要 PLAYWRIGHT_BROWSERS_PATH 或本地 playwright install chromium
    默认访问 http://127.0.0.1
"""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1")
SIZES = [
    ("mobile-390x844", 390, 844),
    ("mobile-430x932", 430, 932),
    ("tablet-768x1024", 768, 1024),
    ("desktop-1440x900", 1440, 900),
]
OUTPUT_DIR = Path(__file__).parent.parent / "screenshots"

# 临时验收账号
EMAIL = "e2e-deploy@temp.local"
PASSWORD = "TempPass2026!"


async def login_and_screenshot() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # 登录
        await page.goto(f"{BASE_URL}/login")
        await page.fill('input[type="email"], input[name="email"], input[placeholder*="邮箱"]', EMAIL)
        await page.fill('input[type="password"], input[name="password"], input[placeholder*="密码"]', PASSWORD)
        await page.click('button:has-text("登录服务台")')
        await page.wait_for_url(f"{BASE_URL}/", timeout=10000)

        for name, width, height in SIZES:
            await page.set_viewport_size({"width": width, "height": height})
            await page.goto(f"{BASE_URL}/")
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(OUTPUT_DIR / f"{name}-homepage.png"), full_page=True)

            await page.goto(f"{BASE_URL}/watchlist")
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(OUTPUT_DIR / f"{name}-watchlist.png"), full_page=True)

            print(f"OK: {name} 截图已保存")

        await browser.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(login_and_screenshot()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
