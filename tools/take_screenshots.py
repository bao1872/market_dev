"""截图脚本 - unify-market-colors-table-watchlist-node spec 端到端验证截图。

用法:
    python3 /root/web_dev/tools/take_screenshots.py

环境:
    - 前端: http://localhost (Nginx :80)
    - 后端 API: http://localhost:8000
    - 依赖: playwright (sync API), jose
    - 输出: /root/trading/<timestamp>-<page>.png (1440x900)

说明:
    通过 JWT 注入 localStorage（auth_token + auth_refresh_token + zustand auth-store）
    模拟登录态，对 5 个页面截图并收集控制台/页面错误，
    用于验证 unify-market-colors-table-watchlist-node spec。
    JWT payload 与 backend/app/core/security.py 一致: {sub, exp, type: "access"}。
    ProtectedLayout 双重校验: isAuthenticated(zustand auth-store) + localStorage.auth_token。
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jose import jwt
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


def find_chromium_executable() -> str | None:
    """自动探测可用的 chromium 可执行文件。

    Python playwright 包版本与缓存中浏览器版本可能不一致 (如期望 -1208, 实有 -1223),
    导致 launch 报 "Executable doesn't exist"。这里在 ms-playwright 缓存中按版本号倒序
    查找 chrome-headless-shell / chrome, 找到则直接通过 executable_path 复用。
    """
    candidates: list[Path] = []
    cache_dirs = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-playwright")),
        Path.home() / ".cache" / "ms-playwright",
    ]
    for cache in cache_dirs:
        if not cache.exists():
            continue
        # headless shell (优先, 体积小启动快)
        candidates.extend(
            sorted(cache.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
                   reverse=True)
        )
        # full chromium (兜底)
        candidates.extend(
            sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
        )
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None

# ===== 配置 =====
# JWT_SECRET 从生产配置文件读取，禁止硬编码
def _load_jwt_secret() -> str:
    """从 /etc/market-dev/config.production.py 读取 JWT_SECRET。

    安全规范: 禁止将真实密钥写入 Git 仓库。
    """
    config_path = "/etc/market-dev/config.production.py"
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"生产配置文件不存在: {config_path}。请确认在生产服务器上运行此脚本。"
        )
    import re
    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^JWT_SECRET\s*=\s*["\'](.+?)["\']\s*$', line)
            if m:
                return m.group(1)
    raise ValueError(f"未在 {config_path} 中找到 JWT_SECRET 配置")


JWT_SECRET = _load_jwt_secret()
JWT_ALGORITHM = "HS256"  # 与 settings.jwt_algorithm 一致

# 测试用户 (DB: users 表, id/email 确认; name/role 在 user_roles 表, member 足以访问用户页)
USER_ID = "0d2fd7cb-d6df-4073-9743-c20ad35c1e04"
USER_EMAIL = "test-user@market.dev"
USER_NAME = "test-user"
USER_ROLE = "member"

BASE_URL = "http://localhost"
OUTPUT_DIR = Path("/root/trading")
STOCK_SYMBOL = "600519"  # 贵州茅台; 路由 /stock/:symbol (非 instrument_id)

VIEWPORT = {"width": 1440, "height": 900}

# 5 个截图目标: (标签, URL, 验证要点)
SCREENSHOTS = [
    ("01-screener", f"{BASE_URL}/screener",
     "右侧'操作'列可见; 股票名带 change_pct; 无 DSA/VWAP 文案"),
    ("02-watchlist", f"{BASE_URL}/watchlist",
     "股票名带实时 change_pct"),
    ("03-stock-detail", f"{BASE_URL}/stock/{STOCK_SYMBOL}",
     "个股详情 Node; profile_meta 区域"),
    ("04-index", f"{BASE_URL}/",
     "首页自选摘要带 change_pct"),
    ("05-monitor", f"{BASE_URL}/monitor",
     "盘中监控页 (注意: App.tsx 无 /monitor 路由, catch-all 会重定向到 /)"),
]


def make_token(token_type: str, ttl_seconds: int) -> str:
    """生成 JWT token。

    payload 与 backend/app/core/security.py 的 create_access_token/create_refresh_token 一致:
        {sub: user_id, exp: 过期时间, type: "access"|"refresh"}
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    payload = {
        "sub": USER_ID,
        "exp": expire,
        "type": token_type,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def build_auth_store_json(access_token: str, refresh_token: str) -> str:
    """构造 zustand persist 的 auth-store JSON。

    与 frontend/src/store/auth.ts 的 partialize 字段一致:
        isAuthenticated, user, token, refreshToken, keepLogin
    zustand persist 存储格式: {"state": {...}, "version": 0}
    """
    state = {
        "isAuthenticated": True,
        "user": {
            "id": USER_ID,
            "name": USER_NAME,
            "email": USER_EMAIL,
            "role": USER_ROLE,
        },
        "token": access_token,
        "refreshToken": refresh_token,
        "keepLogin": True,
    }
    return json.dumps({"state": state, "version": 0})


def capture(page, url: str, out_path: Path) -> dict:
    """导航 -> 等待 -> 截图。

    登录态通过 context.add_init_script 在每个文档脚本执行前注入 localStorage,
    避免"首次导航时 ProtectedLayout 先于注入看到无 token 而跳转 /login"的竞态。
    失败时仍尝试截图保留证据，返回结果字典。
    """
    result = {"url": url, "path": str(out_path), "ok": False, "error": None,
              "final_url": None, "size_bytes": 0}
    try:
        # 1. 导航 (init script 已在脚本前注入 auth, ProtectedLayout 直接放行)
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        result["final_url"] = page.url
        # 2. 等待网络空闲 (部分页面有长轮询/SSE, 超时不阻塞)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass
        # 3. 任务要求: 页面加载后至少等 3 秒再截图
        page.wait_for_timeout(3000)
        # 4. 截图 (仅视口 1440x900, 非 full_page)
        page.screenshot(path=str(out_path), full_page=False)
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        # 失败页面仍尝试截图留证
        try:
            page.screenshot(path=str(out_path), full_page=False)
        except Exception:
            pass
    if out_path.exists():
        result["size_bytes"] = out_path.stat().st_size
    return result


def main() -> int:
    global ACCESS_TOKEN, REFRESH_TOKEN, AUTH_STORE_JSON
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 生成 token: access 1 小时, refresh 7 天 (与 security.py 默认 TTL 对齐)
    ACCESS_TOKEN = make_token("access", 3600)
    REFRESH_TOKEN = make_token("refresh", 7 * 86400)
    AUTH_STORE_JSON = build_auth_store_json(ACCESS_TOKEN, REFRESH_TOKEN)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"[INFO] timestamp={timestamp} user={USER_EMAIL}")
    print(f"[INFO] access_token(前30)={ACCESS_TOKEN[:30]}...")
    print(f"[INFO] output_dir={OUTPUT_DIR}")

    results = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        executable = find_chromium_executable()
        launch_kwargs = {"headless": True}
        if executable:
            launch_kwargs["executable_path"] = executable
            print(f"[INFO] chromium executable: {executable}")
        else:
            print("[INFO] 未探测到 chromium 缓存, 使用 playwright 默认 (可能需 playwright install)")
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport=VIEWPORT)
        # 在每个文档脚本执行前注入登录态, 避免 ProtectedLayout 首次渲染时
        # 因 localStorage 无 token 而跳转 /login 的竞态 (init script 先于页面 JS 运行)
        # 注: Python add_init_script 不接受参数, 需将值以 json.dumps 嵌入 JS 字符串字面量
        init_script = (
            "(() => { try {\n"
            f"  localStorage.setItem('auth_token', {json.dumps(ACCESS_TOKEN)});\n"
            f"  localStorage.setItem('auth_refresh_token', {json.dumps(REFRESH_TOKEN)});\n"
            f"  localStorage.setItem('auth-store', {json.dumps(AUTH_STORE_JSON)});\n"
            "} catch (e) { /* localStorage 不可用时静默, 截图会显示登录页便于诊断 */ } })();"
        )
        context.add_init_script(init_script)
        page = context.new_page()

        # 收集控制台错误与页面异常 (不阻塞截图)
        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}")
                if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda err: page_errors.append(f"{type(err).__name__}: {err}"))

        for label, url, verify_note in SCREENSHOTS:
            out_path = OUTPUT_DIR / f"{timestamp}-{label}.png"
            print(f"\n[SHOT] {label} -> {url}")
            print(f"       验证要点: {verify_note}")
            res = capture(page, url, out_path)
            res["label"] = label
            res["verify_note"] = verify_note
            results.append(res)
            status = "OK" if res["ok"] else "FAIL"
            redirect = ""
            if res["final_url"] and res["final_url"].rstrip("/") != url.rstrip("/"):
                redirect = f" (重定向到 {res['final_url']})"
            print(f"       [{status}]{redirect} size={res['size_bytes']}B"
                  f"{' error=' + res['error'] if res['error'] else ''}")

        browser.close()

    # ===== 汇总 =====
    print("\n" + "=" * 70)
    print("截图汇总")
    print("=" * 70)
    ok_count = sum(1 for r in results if r["ok"])
    print(f"成功 {ok_count}/{len(results)}")
    for r in results:
        flag = "OK" if r["ok"] else "FAIL"
        print(f"  [{flag}] {r['label']}: {r['path']} ({r['size_bytes']}B)")
        if r["error"]:
            print(f"         error: {r['error']}")
        if r["final_url"] and r["final_url"].rstrip("/") != r["url"].rstrip("/"):
            print(f"         注意: 请求 {r['url']} -> 实际 {r['final_url']}")

    if console_errors:
        print(f"\n控制台错误/警告 ({len(console_errors)}):")
        for line in console_errors[:20]:
            print(f"  {line}")
    if page_errors:
        print(f"\n页面异常 ({len(page_errors)}):")
        for line in page_errors[:20]:
            print(f"  {line}")

    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
