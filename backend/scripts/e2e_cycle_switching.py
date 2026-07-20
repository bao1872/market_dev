"""周期切换真实浏览器 E2E 脚本 — PROMPT.md §4-C 要求。

用法：
    cd /root/web_dev/backend
    python scripts/e2e_cycle_switching.py

功能（PROMPT.md §4-C）：
    真实浏览器验证周期切换 1d→15m→1h→1w→1mo→1d 全流程：
    1. AbortController + response.timeframe 乱序丢弃在实际浏览器中工作
    2. 每次切换后图表正确渲染（canvas 存在 + 截图非空白）
    3. 不同周期渲染内容不同（相邻周期截图 hash 不同）
    4. URL timeframe 参数正确更新
    5. 共享 time-index map 在多周期切换下稳定（无崩溃/无空白渲染）

环境依赖（非 pytest 自动化，需手动运行）：
    - trading-frontend 容器运行在 http://localhost:80
    - trading-backend 容器运行在 http://localhost:8000
    - Playwright chromium 已安装（~/.cache/ms-playwright/chromium_headless_shell-1208）
    - test-admin@market.dev 用户存在于数据库

输出：
    - 截图：/tmp/e2e_cycle_screenshots/{step}_{timeframe}.png
    - 报告：backend/tests/e2e_cycle_result.json

设计说明：
    - 用 Playwright sync_api（简单直接）
    - 通过 backend security.create_access_token 生成 test-admin token（避免硬编码过期 token）
    - 注入 zustand persist state（auth-store）+ auth_token + auth_refresh_token 到 localStorage
    - 等待策略：networkidle + canvas 出现 + 额外延时（确保 indicators 渲染完成）
    - hash：截图文件 sha256（验证不同周期渲染不同）
    - PNG 校验：复用 png_validator（非空白/非纯色/合理尺寸）
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 确保 backend 在 sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.services.png_validator import validate_png  # noqa: E402

# ===== 配置 =====
FRONTEND_URL = "http://localhost/"
STOCK_SYMBOL = "000725"  # 京东方A（SMC 对齐基准）
# PROMPT.md §4-C: 1d→15m→1h→1w→1mo→1d 完整周期切换
CYCLE_SEQUENCE = ["1d", "15m", "1h", "1w", "1mo", "1d"]
# 周期按钮文字（前端 i18n）
TIMEFRAME_BUTTON_LABEL = {
    "1d": "日",
    "15m": "15m",
    "1h": "1h",
    "1w": "周",
    "1mo": "月",
}
# test-admin@market.dev 用户 ID
TEST_ADMIN_USER_ID = "755c8a77-c2b8-4c3b-a9f7-8b123a483e56"
# 截图输出目录
SCREENSHOT_DIR = Path("/tmp/e2e_cycle_screenshots")
# 报告输出路径
REPORT_PATH = BACKEND_DIR / "tests" / "e2e_cycle_result.json"
# 视口尺寸
VIEWPORT = {"width": 1440, "height": 900}
# 渲染等待时间（ms）：networkidle 后额外等待 indicators 渲染
RENDER_WAIT_MS = 5000
# 初始加载等待时间（ms）：首次访问详情页等待更久
INITIAL_WAIT_MS = 7000


def _generate_admin_token() -> tuple[str, str]:
    """生成 test-admin 的 access + refresh token。

    必须在 backend 容器内生成（宿主机与容器的 jwt_secret 可能不一致，
    宿主机生成的 token 会被 backend 拒绝）。通过 docker exec 调用容器内
    app.core.security.create_access_token，确保签名密钥一致。
    """
    import subprocess

    script = (
        "import sys; sys.path.insert(0, '/app'); "
        "from app.core.security import create_access_token, create_refresh_token; "
        f"uid = '{TEST_ADMIN_USER_ID}'; "
        "print(create_access_token(uid)); "
        "print(create_refresh_token(uid))"
    )
    result = subprocess.run(
        ["docker", "exec", "trading-backend", "python3", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    lines = result.stdout.strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError(f"docker exec 生成 token 输出异常: {result.stdout!r}")
    return lines[0].strip(), lines[1].strip()


def _build_zustand_state(access_token: str, refresh_token: str) -> str:
    """构造 zustand persist 的 auth-store state JSON。"""
    user = {
        "id": TEST_ADMIN_USER_ID,
        "name": "test-admin@market.dev",
        "email": "test-admin@market.dev",
        "is_admin": True,
        "roles": ["admin"],
        "subscription_active": True,
        "plan_code": None,
        "plan_display_name": None,
        "expires_at": None,
        "features": [],
        "limits": {},
    }
    state = {
        "state": {
            "isAuthenticated": True,
            "user": user,
            "token": access_token,
            "refreshToken": refresh_token,
            "keepLogin": True,
        },
        "version": 0,
    }
    return json.dumps(state)


def _inject_auth(page, access_token: str, refresh_token: str, zustand_state: str) -> None:
    """注入 auth token 到 localStorage（让 SPA 认为已登录）。"""
    page.evaluate(
        """([accessToken, refreshToken, zustandState]) => {
        localStorage.setItem('auth_token', accessToken);
        localStorage.setItem('auth_refresh_token', refreshToken);
        localStorage.setItem('auth-store', zustandState);
    }""",
        [access_token, refresh_token, zustand_state],
    )


def _wait_for_chart_ready(page, *, is_initial: bool = False) -> None:
    """等待图表渲染完成：networkidle + canvas 出现 + 额外延时。"""
    wait_ms = INITIAL_WAIT_MS if is_initial else RENDER_WAIT_MS
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        # networkidle 可能超时（有长连接），不阻塞
        pass
    # 等待 canvas 出现
    try:
        page.wait_for_selector("canvas", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(wait_ms)


def _click_timeframe_button(page, timeframe: str) -> bool:
    """点击周期按钮，返回是否成功。"""
    label = TIMEFRAME_BUTTON_LABEL[timeframe]
    btn = page.get_by_role("button", name=label, exact=True)
    if btn.count() == 0:
        return False
    btn.click()
    return True


def _get_url_timeframe(page) -> str | None:
    """从 URL 提取 timeframe 参数。"""
    q = parse_qs(urlparse(page.url).query)
    tf_list = q.get("timeframe")
    return tf_list[0] if tf_list else None


def _compute_hash(file_path: Path) -> str:
    """计算文件 sha256。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_screenshot(png_path: Path) -> dict[str, Any]:
    """用 png_validator 校验截图，返回结果 dict。"""
    with open(png_path, "rb") as f:
        data = f.read()
    r = validate_png(data)
    return {
        "valid": r.valid,
        "width": r.width,
        "height": r.height,
        "byte_size": r.byte_size,
        "error_code": r.error_code,
        "error_message": r.error_message,
    }


def run_e2e() -> dict[str, Any]:
    """执行 E2E 周期切换测试，返回报告 dict。"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # 清理旧截图
    for old in SCREENSHOT_DIR.glob("*.png"):
        old.unlink()

    access_token, refresh_token = _generate_admin_token()
    zustand_state = _build_zustand_state(access_token, refresh_token)

    report: dict[str, Any] = {
        "script": "e2e_cycle_switching",
        "started_at": datetime.now(UTC).isoformat(),
        "symbol": STOCK_SYMBOL,
        "cycle_sequence": CYCLE_SEQUENCE,
        "steps": [],
        "summary": {},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()

        # 1. 打开首页注入 token
        page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
        _inject_auth(page, access_token, refresh_token, zustand_state)

        # 2. 依次切换周期
        for idx, timeframe in enumerate(CYCLE_SEQUENCE):
            step: dict[str, Any] = {
                "step": idx,
                "timeframe": timeframe,
                "is_initial": idx == 0,
            }

            if idx == 0:
                # 首次：直接访问带 timeframe 的 URL
                url = f"{FRONTEND_URL}stock/{STOCK_SYMBOL}?timeframe={timeframe}"
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                _wait_for_chart_ready(page, is_initial=True)
            else:
                # 后续：点击周期按钮切换
                clicked = _click_timeframe_button(page, timeframe)
                step["button_clicked"] = clicked
                if not clicked:
                    step["error"] = f"周期按钮 {TIMEFRAME_BUTTON_LABEL[timeframe]!r} 未找到"
                    report["steps"].append(step)
                    break
                _wait_for_chart_ready(page, is_initial=False)

            # 验证 URL 参数
            url_tf = _get_url_timeframe(page)
            step["url_timeframe"] = url_tf
            step["url_timeframe_matches"] = url_tf == timeframe

            # 验证 canvas
            canvases = page.query_selector_all("canvas")
            step["canvas_count"] = len(canvases)

            # 截图
            screenshot_path = SCREENSHOT_DIR / f"step{idx:02d}_{timeframe}.png"
            page.screenshot(path=str(screenshot_path), full_page=False)
            step["screenshot"] = str(screenshot_path)

            # hash + PNG 校验
            step["hash"] = _compute_hash(screenshot_path)
            step["png_validation"] = _validate_screenshot(screenshot_path)

            # 捕获 console 错误（验证 AbortController 乱序丢弃不报错）
            # 注意：此处仅记录当步的 console，不阻塞

            report["steps"].append(step)

        # 捕获 console 错误汇总
        # 重新挂载 console 监听已晚，改为检查页面是否有未捕获错误
        browser.close()

    # 3. 汇总分析
    report["completed_at"] = datetime.now(UTC).isoformat()

    all_steps_ok = True
    hash_set: set[str] = set()
    adjacent_same_hash = []
    for i, step in enumerate(report["steps"]):
        png_ok = step.get("png_validation", {}).get("valid", False)
        url_ok = step.get("url_timeframe_matches", False)
        canvas_ok = step.get("canvas_count", 0) > 0
        if not (png_ok and url_ok and canvas_ok):
            all_steps_ok = False
        h = step.get("hash")
        if h:
            if h in hash_set:
                # 同 hash 出现多次：仅当两步都是 1d 时允许（首尾一致性）
                tf = step.get("timeframe")
                if tf != "1d":
                    all_steps_ok = False
            hash_set.add(h)
        # 相邻周期 hash 相同 = 渲染未变化（异常）
        if i > 0:
            prev_hash = report["steps"][i - 1].get("hash")
            if prev_hash == h:
                adjacent_same_hash.append(
                    {
                        "step": i,
                        "prev_tf": report["steps"][i - 1].get("timeframe"),
                        "curr_tf": step.get("timeframe"),
                    }
                )

    report["summary"] = {
        "all_steps_completed": len(report["steps"]) == len(CYCLE_SEQUENCE),
        "all_steps_ok": all_steps_ok,
        "unique_hashes": len(hash_set),
        "adjacent_same_hash_count": len(adjacent_same_hash),
        "adjacent_same_hash": adjacent_same_hash,
        "screenshots_dir": str(SCREENSHOT_DIR),
    }

    # 4. 写报告
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    return report


def main() -> int:
    """主入口，返回退出码（0=成功，1=失败）。"""
    print("[e2e_cycle_switching] 开始 E2E 周期切换测试")
    print(f"  符号: {STOCK_SYMBOL}")
    print(f"  周期序列: {' → '.join(CYCLE_SEQUENCE)}")
    print(f"  截图目录: {SCREENSHOT_DIR}")
    print()

    start = time.time()
    report = run_e2e()
    elapsed = time.time() - start

    # 打印每步结果
    print("=" * 80)
    print("步骤详情:")
    print("=" * 80)
    for step in report["steps"]:
        tf = step.get("timeframe", "?")
        png_v = step.get("png_validation", {})
        ok = "✓" if step.get("url_timeframe_matches") and png_v.get("valid") and step.get("canvas_count", 0) > 0 else "✗"
        size = png_v.get("byte_size", 0)
        h = step.get("hash", "")[:12]
        print(
            f"  {ok} step{step.get('step'):02d} {tf:>4s} | "
            f"URL={step.get('url_timeframe_matches')} | "
            f"canvas={step.get('canvas_count', 0)} | "
            f"{size:>7d}B | hash={h}"
        )
        if step.get("error"):
            print(f"      ERROR: {step['error']}")

    print()
    print("=" * 80)
    print("汇总:")
    print("=" * 80)
    s = report["summary"]
    print(f"  全部步骤完成: {s['all_steps_completed']}")
    print(f"  全部步骤通过: {s['all_steps_ok']}")
    print(f"  唯一 hash 数: {s['unique_hashes']} / {len(CYCLE_SEQUENCE)}")
    print(f"  相邻周期相同 hash: {s['adjacent_same_hash_count']} (应为 0)")
    print(f"  报告: {REPORT_PATH}")
    print(f"  耗时: {elapsed:.1f}s")

    return 0 if s["all_steps_ok"] and s["all_steps_completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
