"""问财（同花顺 iwencai）统一 HTTP 客户端。

背景：
- `pywencai` 第三方库已失效（封装解析不了问财新版 `get-robot-data` 响应结构，
  即使传入有效 cookie 也返回 None + 验证码 401）。
- 本项目改为直接走问财底层 HTTP 接口，绕过失效库。本模块是板块同步与
  竞价回补共用的统一客户端。

Cookie 管理（用户规则 2026-08-17）：
- 问财 cookie 有有效期（sess_tk 约 7 天、v 约 1 年）。过期后用户会从浏览器
  重新复制 cookie 文本给助手。
- 助手用 `parse_cookie_input` 解析任意格式的用户粘贴文本（浏览器 Cookie
  表格复制 / `name=value;...` 串），标准化后写入 **JSON 文件** `wencai_cookie.json`
  （已被 .gitignore 忽略，不进版本库）。
- 设计意图：cookie 不进 `.env`（`.env` 不会进容器镜像，且 compose 不注入），
  改为独立 JSON 文件。本地放 `backend/wencai_cookie.json`；服务器通过
  `docker cp` 复制到容器内 `/app/wencai_cookie.json`（应用 cwd 为 /app）。
  更新 cookie 时只需把本地 JSON 文件复制到服务器容器即可，无需改 market.env
  或重启整个 deploy。
- 运行时统一从 `load_cookie()` 读取，优先级：
  env `WENCAI_COOKIE` → 本地 `backend/wencai_cookie.json`
  → 容器内 `/app/wencai_cookie.json` → 兼容旧 `backend/.env` 的 `WENCAI_COOKIE`。

脱敏：本模块不记录 cookie 原文或完整响应内容到日志。

限流：问财对非登录/高频访问有限流。竞价回补 120 天逐日问句必须随机间隔
30–60 秒（每次「问句之间」），由调用方通过 `QUERY_INTERVAL_RANGE` / 问句间
sleep 控制；同一问句内翻页用 `PAGE_DELAY_RANGE`（1–2s）短间隔。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 本地开发：backend/wencai_cookie.json（gitignore）
_COOKIE_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "wencai_cookie.json"
# 容器内可读路径（docker cp 复制目标；应用 cwd=/app）
_COOKIE_JSON_PATH_IN_CONTAINER = Path("/app/wencai_cookie.json")
# 兼容旧的 backend/.env 写法（仅回退读取，不再写入）
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

_ROBOT_URL = "http://www.iwencai.com/customized/chart/get-robot-data"
_DATALIST_URL = "http://www.iwencai.com/gateway/urp/v7/landing/getDataList"

_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "http://www.iwencai.com/",
    "Content-Type": "application/json",
}

# 每次「问句之间」的随机间隔（秒）：30–60 秒（用户硬性限流规则，回补逐日/板块同步均适用）
QUERY_INTERVAL_RANGE = (30.0, 60.0)
# 同一问句内「翻页之间」的短间隔（秒）：仅避免瞬时连发，非 30–60s 规则
PAGE_DELAY_RANGE = (1.0, 2.0)


def parse_cookie_input(raw: str) -> str:
    """把用户粘贴的任意 cookie 文本标准化为 `name=value; name=value` 串。

    支持两种来源格式：
    1. 浏览器 DevTools → Application → Cookies 表格复制（Tab 分隔多列：
       `name\\tvalue\\thost\\tpath\\texpiry\\tsize\\t...`）
    2. 浏览器 Console `document.cookie` 或 Request Headers 的
       `name=value; name=value; ...` 串

    Args:
        raw: 用户粘贴的原始 cookie 文本

    Returns:
        标准化后的 cookie 串（`name1=v1; name2=v2; ...`）。
        仅保留 name=value 对，剔除空值。
    """
    if not raw:
        return ""

    # 情况 2：已含 '=' 且含 ';' 或仅 name=value 串
    # 先按行拆分（表格复制常为换行分隔的行）
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    pairs: dict[str, str] = {}

    for line in lines:
        # 表格格式：Tab 分隔，第一列 name，第二列 value
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] and parts[1]:
                pairs[parts[0].strip()] = parts[1].strip()
            continue

        # 串格式：name=value; name=value
        # 按 ';' 拆分，每段再按首个 '=' 拆分
        for seg in line.split(";"):
            seg = seg.strip()
            if not seg or "=" not in seg:
                continue
            k, _, v = seg.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                pairs[k] = v

    if not pairs:
        return ""

    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def _read_cookie_from_json(path: Path) -> str | None:
    """从 wencai_cookie.json 读取 cookie 串（兼容性/容器路径回退）。"""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        cookie = data.get("cookie")
        return cookie.strip() if isinstance(cookie, str) and cookie.strip() else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _read_cookie_from_env_file() -> str | None:
    """兼容旧写法：从 .env 文件解析 WENCAI_COOKIE（仅回退读取，不再写入）。"""
    if not _ENV_PATH.exists():
        return None
    try:
        with _ENV_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("WENCAI_COOKIE="):
                    val = line[len("WENCAI_COOKIE="):].strip().strip('"').strip("'")
                    return val or None
    except OSError:
        return None
    return None


def load_cookie() -> str | None:
    """读取问财 cookie 串，按优先级：

    1. 环境变量 `WENCAI_COOKIE`
    2. 本地 `backend/wencai_cookie.json`
    3. 容器内 `/app/wencai_cookie.json`（docker cp 复制目标）
    4. 兼容旧 `backend/.env` 的 `WENCAI_COOKIE`

    Returns:
        标准化 cookie 串；均未配置返回 None。
    """
    cookie = os.getenv("WENCAI_COOKIE")
    if cookie and cookie.strip():
        return cookie.strip()

    for path in (_COOKIE_JSON_PATH, _COOKIE_JSON_PATH_IN_CONTAINER):
        val = _read_cookie_from_json(path)
        if val:
            return val

    fallback = _read_cookie_from_env_file()
    return fallback.strip() if fallback else None


def save_cookie_to_json(cookie_str: str, updated_by: str = "user-paste") -> Path:
    """把标准化后的 cookie 串写入本地 `backend/wencai_cookie.json`。

    该文件已被 .gitignore 忽略，不会进入版本库（敏感凭据本地留存）。
    服务器侧：把本文件 `docker cp` 到容器内 `/app/wencai_cookie.json` 即可，
    无需改 market.env 或重启 deploy。

    Args:
        cookie_str: parse_cookie_input 产出的标准 cookie 串
        updated_by: 来源标记（默认 "user-paste"，便于追溯）

    Returns:
        写入的 JSON 路径
    """
    cookie_str = cookie_str.strip()
    if not cookie_str:
        raise ValueError("cookie 串为空，拒绝写入 wencai_cookie.json")

    payload = {
        "cookie": cookie_str,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": updated_by,
        "note": "问财(iwencai)登录态 cookie；过期后由用户重新复制粘贴更新。",
    }
    with _COOKIE_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    logger.info("[WencaiClient] 已更新 %s", _COOKIE_JSON_PATH)
    return _COOKIE_JSON_PATH


def _get_footer_url(cookie: str, query: str) -> tuple[str, int]:
    """第一步：请求 get-robot-data，从响应中提取表格数据 URL 与总行数。

    Returns:
        (getDataList 完整 URL, row_count)

    Raises:
        RuntimeError: 问财返回无效响应（cookie 过期 / 被限流 / 问句无法解析）
    """
    payload = {
        "add_info": '{"urp":{"scene":1,"company":1,"business":1},'
                    '"contentType":"json","searchInfo":true}',
        "perpage": "10",
        "page": 1,
        "source": "Ths_iwencai_Xuangu",
        "log_info": '{"input_type":"click"}',
        "version": "2.0",
        "secondary_intent": "stock",
        "question": query,
    }
    headers = {**_HEADERS_BASE, "Cookie": cookie}
    r = requests.post(_ROBOT_URL, json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"get-robot-data HTTP {r.status_code}")
    j = r.json()
    try:
        content = j["data"]["answer"][0]["txt"][0]["content"]
        if isinstance(content, str):
            content = __import__("json").loads(content)
        comps = content.get("components", [])
        if not comps:
            raise KeyError("no components")
        comp = comps[0]
        footer = (
            comp.get("config", {})
            .get("other_info", {})
            .get("footer_info", {})
            .get("url")
        )
        row_count = (
            comp.get("data", {})
            .get("meta", {})
            .get("extra", {})
            .get("row_count", 0)
        )
        if not footer:
            raise KeyError("no footer_url")
        return footer, int(row_count or 0)
    except (KeyError, IndexError, TypeError) as e:
        # cookie 过期时问财返回验证码/空 answer，这里统一报错
        raise RuntimeError(
            f"问财响应缺少表格数据（cookie 可能过期或被限流）: {e}"
        ) from e


def _fetch_page(footer_url: str, cookie: str, page: int, perpage: int) -> list[dict]:
    """拉取单页表格数据（datas 列表）。"""
    # 问财返回相对路径，补主机前缀
    if footer_url.startswith("/"):
        footer_url = "http://www.iwencai.com" + footer_url
    sep = "&" if "?" in footer_url else "?"
    url = f"{footer_url}{sep}page={page}&perpage={perpage}"
    headers = {**_HEADERS_BASE, "Cookie": cookie}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"getDataList HTTP {r.status_code} (page={page})")
    j = r.json()
    try:
        data = j["answer"]["components"][0]["data"]
        return data.get("datas") or []
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"getDataList 响应解析失败: {e}") from e


def fetch_query_table(
    query: str,
    cookie: str | None = None,
    perpage: int = 100,
    max_pages: int | None = None,
    page_delay_range: tuple[float, float] = PAGE_DELAY_RANGE,
) -> list[dict]:
    """执行一个问财问句，返回全部行的 list[dict]。

    流程：get-robot-data（拿 footer_url + row_count）→ 翻页 getDataList。

    注意限流语义（用户规则 2026-08-17）：
    - 本函数的 `page_delay_range` 仅控制**同一问句内翻页之间**的短间隔
      （默认 1–2 秒），避免瞬时连发被风控。
    - **问句之间（如回补逐日）的 30–60 秒随机间隔**不在此处，
      由调用方在两次 `fetch_query_table` 调用之间 sleep（见 QUERY_INTERVAL_RANGE）。
      切勿把 30–60s 当成页间间隔，否则单问句多页会被错误拖慢。

    Args:
        query: 自然语言问句（如 "20260814竞价涨幅" / "同花顺概念，行业分类"）
        cookie: 标准化 cookie 串；None 时从 load_cookie() 读取
        perpage: 每页行数（问财上限通常 100）
        max_pages: 安全上限；None 按 row_count 推算
        page_delay_range: 同一问句内翻页之间的随机间隔（秒）

    Returns:
        全部数据行（list of dict，键为中文字段名）

    Raises:
        RuntimeError: cookie 缺失 / 问财返回无效
    """
    cookie = cookie or load_cookie()
    if not cookie:
        raise RuntimeError(
            "未配置 WENCAI_COOKIE：请写入 wencai_cookie.json，或设置环境变量 WENCAI_COOKIE"
        )

    footer_url, row_count = _get_footer_url(cookie, query)
    if row_count <= 0:
        logger.warning("[WencaiClient] 问句 %r 返回 0 行", query)
        return []

    import math
    total_pages = max(1, math.ceil(row_count / perpage))
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    logger.info(
        "[WencaiClient] 问句 %r: row_count=%d, pages=%d, perpage=%d",
        query, row_count, total_pages, perpage,
    )

    all_rows: list[dict] = []
    for page in range(1, total_pages + 1):
        rows = _fetch_page(footer_url, cookie, page, perpage)
        all_rows.extend(rows)
        if page < total_pages:
            delay = random.uniform(*page_delay_range)
            time.sleep(delay)

    logger.info("[WencaiClient] 问句 %r 共拉取 %d 行", query, len(all_rows))
    return all_rows
