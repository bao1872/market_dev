"""问财（同花顺 iwencai）统一 HTTP 客户端。

背景：
- `pywencai` 第三方库已失效（封装解析不了问财新版响应结构，即使传入有效 cookie
  也返回 None + 验证码 401）。
- [WENCAI-STREAM-QUERY-MIGRATION-01] 旧的两跳协议
  `get-robot-data` → `getDataList` 已被问财下线：实测 http/https **均返回
  HTML 403 Forbidden**（与 cookie 有效性无关，换 https / 补 Referer / 补
  hexin-v 都不能恢复）。
  现改用浏览器实测可用的**单跳 SSE 协议**：
      POST https://www.iwencai.com/gateway/aime/stream-query
      → text/event-stream → `other/openAnswer` 帧内直接含全表 datas
- 调用方（`wencai_board_provider`）**不需要理解 SSE**：本模块继续以
  `list[dict]` 暴露结果（`fetch_query_table`），业务层零改动。

调用点（2026-09-24 实核）：`fetch_query_table` 目前唯一生产调用方是
`wencai_board_provider._fetch_wencai_sync`；竞价/回补相关代码已不再引用本模块。

Cookie 管理（用户规则 2026-08-17）：
- 问财 cookie 有有效期（sess_tk 约 7 天、v 约 1 年）。过期后用户会从浏览器
  重新复制 cookie 文本给助手。
- 助手用 `parse_cookie_input` 解析任意格式的用户粘贴文本（浏览器 Cookie
  表格复制 / `name=value;...` 串），标准化后写入 **JSON 文件** `wencai_cookie.json`
  （已被 .gitignore 忽略，不进版本库）。
- 设计意图：cookie 不进 `.env`（`.env` 不会进容器镜像，且 compose 不注入），
  改为独立 JSON 文件，本地放 `backend/wencai_cookie.json`（已 gitignore）。
- [BOARD-LOCAL-OWNERSHIP-01] **板块/概念生产同步的 Wencai cookie 仅由本地 Mac
  使用；生产服务器不持有该 cookie。** 生产侧只接收本地构造好的规范化快照
  （见 `board_snapshot_transfer` / `cli/board_snapshot_import`），不再存在
  "把 cookie 复制到服务器容器" 的流程。
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

# ── [WENCAI-STREAM-QUERY-MIGRATION-01] 冻结的外部协议常量（浏览器实测）────────
# 这些**不是**业务参数，而是问财当前协议合同；协议变化时只改本文件这一处 owner。
# 依据：Chrome "Copy as cURL" 原样重放 + 受控剥离实测（2026-09-24）。
# 不进 .env / config.py / DB / admin 设置。
_STREAM_QUERY_URL = "https://www.iwencai.com/gateway/aime/stream-query"
_STREAM_QUERY_SOURCE = "ths_iwencai_pc_xuangu"
_STREAM_QUERY_DIALOG_MODEL = "CUSTOMER_AGENT"
_STREAM_QUERY_VERSION = "3.4.1"
_STREAM_QUERY_AGENT_ID = "MaSzyUwyyl"
_STREAM_QUERY_TOOL_ID = "FinQuery"
_STREAM_QUERY_DOMAIN = "stock"
_STREAM_QUERY_DEVICE_TYPE = "android"

#: 单次全表请求的 perpage。浏览器实测 perpage=6000 一次取回 5578 行（1.64MB）。
#: 这不是问财「永久 API 上限」，只是**当前经过真实验证的最大安全取值**：
#: row_count > 本值，或 len(datas) != row_count，必须 fail closed（禁止静默截断，
#: 也禁止自动猜新的分页协议）。
_STREAM_QUERY_MAX_ROWS = 6000

#: 全表响应实测约 1.6MB，超时需明显大于旧两跳协议的 30s。
_STREAM_QUERY_TIMEOUT_SECONDS = 120

_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    # [MIGRATION-01] Referer 已删除：实测新端点不依赖它（旧 Referer 补丁是
    # get-robot-data 时代的 WAF 绕法，现已无意义）。缺失 User-Agent 才会 403。
    "Content-Type": "application/json",
}


class WencaiTransportError(RuntimeError):
    """传输层失败（非 200 / 应用层 access 拒绝 / SSE 结构不可用）。"""


class WencaiIncompleteResultError(RuntimeError):
    """结果不完整：row_count 超出已验证的单次容量，或 len(datas) != row_count。

    必须 fail closed —— 禁止静默返回部分行（部分行会被下游当作完整表写入 PIT）。
    """

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


def _build_stream_query_payload(query: str) -> dict[str, Any]:
    """构造 stream-query 请求体。

    仅 `question` 为动态值；其余字段是浏览器实测的**冻结协议结构**，
    未经新的证据不得精简、重排或改名。`perpage` 恒为
    `_STREAM_QUERY_MAX_ROWS`（单次全表请求，不再分页）。
    """
    return {
        "question": query,
        "default_fallback": False,
        "input_type": "click",
        "entity_info": {
            "device_type": _STREAM_QUERY_DEVICE_TYPE,
            "comefrom": None,
        },
        "source": _STREAM_QUERY_SOURCE,
        "dialog_model": _STREAM_QUERY_DIALOG_MODEL,
        "version": _STREAM_QUERY_VERSION,
        "agent_tools": [
            {
                "tool_id": _STREAM_QUERY_TOOL_ID,
                "tool_param": {
                    "domain": _STREAM_QUERY_DOMAIN,
                    "perpage": _STREAM_QUERY_MAX_ROWS,
                },
            }
        ],
        "events": [
            {
                "event_type": "user_input",
                "event_name": "normal_agent",
                "content": {},
            }
        ],
        "add_info": {},
        "agent_id": _STREAM_QUERY_AGENT_ID,
        "agent_name": "",
    }


def _parse_stream_query_frames(raw_text: str) -> tuple[list[dict], int]:
    """解析 SSE `data:` 帧，提取全表 `datas` 与权威 `row_count`。

    只处理 `data:` JSON 帧（浏览器实测帧均为该形态）。**不使用**
    `has_more_data` 判定完整性 —— 实测整表返回时它仍为 true。

    Raises:
        WencaiTransportError: 应用层 access 拒绝 / 缺 openAnswer / 结构非法。
    """
    frames: list[dict[str, Any]] = []
    for line in (raw_text or "").splitlines():
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body:
            continue
        try:
            frame = json.loads(body)
        except json.JSONDecodeError as exc:
            # 浏览器证据中不存在非 JSON 的 data: 控制帧 → fail closed（不静默忽略）
            raise WencaiTransportError(
                f"stream-query SSE data 帧非法（fail closed）: {exc}"
            ) from exc
        if isinstance(frame, dict):
            frames.append(frame)

    # 应用层拒绝优先识别：可读原因远胜旧协议的空洞 HTTP 403
    for frame in frames:
        if frame.get("answer_path") == "access/forbidden":
            extra = frame.get("extra") or {}
            raise WencaiTransportError(
                "stream-query 应用层拒绝: answer_path=access/forbidden "
                f"message={extra.get('message')!r}"
            )

    for frame in frames:
        if frame.get("answer_path") != "other/openAnswer":
            continue
        section = frame.get("section") or {}
        components = ((section.get("result_page") or {}).get("components")) or []
        if not components:
            raise WencaiTransportError("stream-query openAnswer 缺少 components")
        data = components[0].get("data") or {}
        datas = data.get("datas")
        row_count = ((data.get("meta") or {}).get("extra") or {}).get("row_count")

        if not isinstance(datas, list):
            raise WencaiTransportError(
                f"stream-query datas 不是列表（fail closed）: {type(datas).__name__}"
            )
        for idx, item in enumerate(datas):
            if not isinstance(item, dict):
                raise WencaiTransportError(
                    f"stream-query datas[{idx}] 不是 dict（fail closed）: "
                    f"{type(item).__name__}"
                )
        if not isinstance(row_count, int) or isinstance(row_count, bool):
            raise WencaiTransportError(
                f"stream-query row_count 缺失或非整数（fail closed）: {row_count!r}"
            )
        return datas, row_count

    raise WencaiTransportError(
        "stream-query 未返回 other/openAnswer 帧（fail closed）"
    )


def _stream_query(cookie: str, query: str) -> tuple[list[dict], int]:
    """执行一次 stream-query（单跳 SSE），返回 (全表行, 权威 row_count)。

    必需请求头经浏览器剥离实测确定：User-Agent + Cookie + Content-Type +
    Accept: text/event-stream。不发送 Referer / Origin / X-Source / hexin-v
    （实测均非必需；且 hexin-v 不单独派生）。

    Raises:
        WencaiTransportError: HTTP 非 200 / 应用层拒绝 / SSE 结构非法。
    """
    headers = {
        **_HEADERS_BASE,
        "Accept": "text/event-stream",
        "Cookie": cookie,
    }
    r = requests.post(
        _STREAM_QUERY_URL,
        json=_build_stream_query_payload(query),
        headers=headers,
        timeout=_STREAM_QUERY_TIMEOUT_SECONDS,
    )
    if r.status_code != 200:
        # 不记录响应正文（可能含风控/敏感内容）
        raise WencaiTransportError(
            f"stream-query HTTP {r.status_code}（transport 失败）"
        )
    return _parse_stream_query_frames(r.text)


def fetch_query_table(
    query: str,
    cookie: str | None = None,
    perpage: int = 100,
    max_pages: int | None = None,
    page_delay_range: tuple[float, float] = PAGE_DELAY_RANGE,
) -> list[dict]:
    """执行一个问财问句，返回**完整表**的 list[dict]。

    [WENCAI-STREAM-QUERY-MIGRATION-01] 流程简化为**单跳**：
        POST stream-query（SSE）→ 解析 other/openAnswer 帧 → 直接得到全表 datas
    旧的两跳流程（get-robot-data 拿 footer_url → 翻页 getDataList）已随问财
    下线该端点而移除，**不保留回退**。

    限流语义（用户规则 2026-08-17，仍然有效）：
    - 单次问句只发**一个** HTTP 请求，不再有页间 sleep。
    - **问句之间**（如历史回补逐日）的 30–60 秒随机间隔仍由**调用方**在两次
      `fetch_query_table` 调用之间 sleep（见 QUERY_INTERVAL_RANGE）；
      本函数内部不引入任何新的等待。

    Args:
        query: 自然语言问句（如 "同花顺概念，行业分类"）
        cookie: 标准化 cookie 串；None 时从 load_cookie() 读取
        perpage: **兼容性保留**（旧分页语义）。stream-query 内部恒用
            `_STREAM_QUERY_MAX_ROWS` 单次取全表，不再按页切分；不得据此
            推断结果被截断。
        max_pages: **兼容性保留**。旧语义为"安全上限/部分页"，新协议无分页，
            故本参数不再影响结果；生产调用方均未传非默认值（2026-09-24 实核）。
        page_delay_range: **兼容性保留**（旧页间短间隔）。新协议单次请求，
            不再产生页间 sleep。

    Returns:
        **完整**表格数据行（list of dict，键为中文字段名）。
        语义不变："返回该问句的完整表"。

    Raises:
        RuntimeError: cookie 缺失
        WencaiTransportError: 传输层失败 / 应用层拒绝 / SSE 结构非法
        WencaiIncompleteResultError: row_count 超出已验证单次容量，
            或 len(datas) != row_count（fail closed，禁止静默截断）
    """
    cookie = cookie or load_cookie()
    if not cookie:
        raise RuntimeError(
            "未配置 WENCAI_COOKIE：请写入 wencai_cookie.json，或设置环境变量 WENCAI_COOKIE"
        )

    rows, row_count = _stream_query(cookie, query)

    if row_count <= 0:
        logger.warning("[WencaiClient] 问句 %r 返回 0 行", query)
        return []

    # ── 全表完整性 SSOT：row_count vs len(datas) ──────────────────────────
    # 不使用 has_more_data：实测整表返回时它仍为 true，不可作为完成判据。
    if row_count > _STREAM_QUERY_MAX_ROWS:
        raise WencaiIncompleteResultError(
            f"问句 {query!r} row_count={row_count} 超过已验证的单次完整表容量 "
            f"{_STREAM_QUERY_MAX_ROWS}（需升级 stream-query 协议，禁止静默截断）"
        )
    if len(rows) != row_count:
        raise WencaiIncompleteResultError(
            f"问句 {query!r} 结果不完整: len(datas)={len(rows)} != row_count="
            f"{row_count}（fail closed，禁止写入部分行）"
        )

    logger.info(
        "[WencaiClient] 问句 %r: row_count=%d, rows=%d（单次全表 stream-query）",
        query, row_count, len(rows),
    )
    return rows
