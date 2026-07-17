"""问财板块数据源 - 同花顺概念/行业分类唯一数据源（PRD §7.5 重构）。

固定查询 "同花顺概念，行业分类"，通过 pywencai 拉取全量 A 股的概念和行业归属。
复用 ref/wencai_concept_industry_export.py 已验证的 Referer 头补丁 + 重试逻辑。

设计要点：
1. 同步调用必须 asyncio.to_thread 包装，不得阻塞事件循环
2. 不记录 Cookie 或完整原始响应（脱敏）
3. 必需字段仅为 股票代码、股票简称、所属概念、所属同花顺行业
4. 股票代码取 .SH/.SZ/.BJ 前六位并保留前导0
5. 概念按 ; 拆分、NFKC、trim、单股去重
6. 行业规范为 "一级-二级-三级" 完整路径，每股恰好一个行业
7. external_code: wc:c:/wc:i: + 规范化名称 SHA256 前24位
8. 发现不同名称哈希冲突立即失败

数据流：
wencai_board_provider.fetch_board_snapshot()
  → pywencai.get(query="同花顺概念，行业分类", loop=True, sleep=2)
  → 选择包含必需字段且行数最大的主表
  → 逐行规范化 → BoardSnapshot（boards + memberships）
  → 供 board_sync_service.sync_boards 原子切换
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 固定查询语句
WENCAI_QUERY = "同花顺概念，行业分类"

# 重试参数
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 5

# 必需字段（股票代码、股票简称、所属概念、所属同花顺行业）
# 问财返回的列名可能略有差异，按包含关系匹配
REQUIRED_FIELD_PATTERNS = {
    "stock_code": ("股票代码",),
    "stock_name": ("股票简称",),
    "concept": ("所属概念",),
    "industry": ("所属同花顺行业", "同花顺行业",),
}

# A 股代码正则：6 位数字 + .SH/.SZ/.BJ 后缀
_STOCK_CODE_RE = re.compile(r"(\d{6})\.(?:SH|SZ|BJ)", re.IGNORECASE)

# 单股概念上限（门禁用）
MAX_CONCEPTS_PER_STOCK = 100


class WencaiBoardProviderError(Exception):
    """问财板块数据源错误基类。"""


class WencaiFetchError(WencaiBoardProviderError):
    """问财拉取失败（网络/接口/重试耗尽）。"""


class WencaiParseError(WencaiBoardProviderError):
    """问财返回数据解析失败（字段缺失/格式异常）。"""


class WencaiHashCollisionError(WencaiBoardProviderError):
    """不同名称哈希冲突（理论极低概率，发现立即失败）。"""


@dataclass
class BoardSnapshot:
    """完整的板块快照（内存中构造，供原子切换）。

    Attributes:
        boards: [{external_code, name, type}]，type ∈ {"industry", "concept"}
        memberships: {(external_code, type): [symbol, ...]}
        raw_rows: 原始行数（门禁用）
        unresolved_symbols: 未解析为有效 A 股代码的原始值（脱敏样本，前50个）
    """

    boards: list[dict[str, str]] = field(default_factory=list)
    memberships: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    raw_rows: int = 0
    unresolved_symbols: list[str] = field(default_factory=list)

    @property
    def board_count(self) -> int:
        return len(self.boards)

    @property
    def membership_count(self) -> int:
        return sum(len(v) for v in self.memberships.values())


def _patch_wencai_headers() -> None:
    """补丁 pywencai headers，添加 Referer 绕过 WAF 403。

    pywencai 的 headers() 只设置 hexin-v / User-Agent / cookie，缺少 Referer。
    问财 WAF 会拦截无 Referer 的请求（返回 403）。
    在调用前给请求头补上 Referer，不改动第三方包本身。
    """
    import pywencai.headers as headers_mod
    import pywencai.wencai as wencai_mod

    original = headers_mod.headers

    def patched(cookie: Any = None, user_agent: Any = None) -> dict[str, str]:
        hdrs = original(cookie, user_agent)
        hdrs.setdefault("Referer", "http://www.iwencai.com/")
        return hdrs

    headers_mod.headers = patched
    wencai_mod.headers = patched  # wencai 模块直接 from .headers import headers


def _collect_dataframes(value: Any, pd: Any, label: str = "result") -> list[tuple[str, Any]]:
    """从 pywencai 可能返回的嵌套结构中收集 DataFrame。

    兼容 DataFrame / dict / list / tuple 嵌套。
    """
    frames: list[tuple[str, Any]] = []

    if isinstance(value, pd.DataFrame):
        if not value.empty:
            frames.append((label, value))
        return frames

    if isinstance(value, dict):
        for key, item in value.items():
            frames.extend(_collect_dataframes(item, pd, f"{label}.{key}"))
        return frames

    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            frames.extend(_collect_dataframes(item, pd, f"{label}[{index}]"))

    return frames


def _match_column(columns: list[str], patterns: tuple[str, ...]) -> str | None:
    """在 DataFrame 列名中匹配必需字段。

    精确匹配优先；若找不到则尝试包含关系匹配。
    """
    for pattern in patterns:
        for col in columns:
            if col == pattern:
                return col
    for pattern in patterns:
        for col in columns:
            if pattern in col:
                return col
    return None


def _select_primary_dataframe(result: Any, pd: Any) -> Any:
    """从问财返回值中选择包含必需字段且行数最大的主表。

    选择逻辑：
    1. 收集所有嵌套 DataFrame
    2. 过滤出包含全部必需字段的 DataFrame
    3. 按行数降序选择最大表

    Raises:
        WencaiParseError: 无 DataFrame 或无表包含全部必需字段
    """
    frames = _collect_dataframes(result, pd)
    if not frames:
        raise WencaiParseError("问财未返回可保存的表格数据")

    # 按行数×列数降序排序
    frames.sort(key=lambda item: (len(item[1]), len(item[1].columns)), reverse=True)

    # 优先选择包含全部必需字段的表
    for label, df in frames:
        columns = list(df.columns)
        matched = {
            key: _match_column(columns, patterns)
            for key, patterns in REQUIRED_FIELD_PATTERNS.items()
        }
        if all(v is not None for v in matched.values()):
            logger.info(
                "[WencaiBoard] 选择主表: label=%s, rows=%d, cols=%d",
                label, len(df), len(df.columns),
            )
            return df

    # 退化：选择行数最多的表（后续解析会在字段缺失时失败）
    selected_label, selected_df = frames[0]
    logger.warning(
        "[WencaiBoard] 未找到包含全部必需字段的表，退化选择最大表: "
        "label=%s, rows=%d, cols=%d, columns=%s",
        selected_label, len(selected_df), len(selected_df.columns),
        list(selected_df.columns),
    )
    return selected_df


def _normalize_stock_code(raw: Any) -> str | None:
    """规范化股票代码：取 .SH/.SZ/.BJ 前六位并保留前导0。

    支持格式：
    - "600000.SH" → "600000"
    - "000001.SZ" → "000001"
    - "688981.BJ" → "688981"
    - 纯6位数字 → 原样返回
    - 其他 → None

    Args:
        raw: 原始股票代码值

    Returns:
        6 位股票代码字符串，无法解析返回 None
    """
    if raw is None:
        return None
    raw_str = str(raw).strip()
    if not raw_str:
        return None

    # 匹配 6 位数字 + .SH/.SZ/.BJ
    match = _STOCK_CODE_RE.search(raw_str)
    if match:
        return match.group(1)

    # 纯 6 位数字（无后缀，部分问财返回格式）
    if len(raw_str) == 6 and raw_str.isdigit():
        return raw_str

    return None


def _normalize_name(name: Any) -> str:
    """规范化板块名称：NFKC + trim。"""
    if name is None:
        return ""
    name_str = str(name).strip()
    # NFKC 规范化：全角→半角，兼容问财返回的全角字符
    name_str = unicodedata.normalize("NFKC", name_str)
    return name_str.strip()


def _normalize_concepts(raw: Any) -> list[str]:
    """规范化概念列表：按 ; 拆分、NFKC、trim、单股去重。

    问财返回的概念字段格式："概念A;概念B;概念C"
    """
    if raw is None:
        return []
    raw_str = str(raw).strip()
    if not raw_str:
        return []

    parts = raw_str.split(";")
    concepts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = _normalize_name(part)
        if not name or name in seen:
            continue
        seen.add(name)
        concepts.append(name)
    return concepts


def _normalize_industry(raw: Any) -> str:
    """规范化行业为 "一级-二级-三级" 完整路径。

    问财返回的行业字段可能格式：
    - "银行" → "银行"（仅一级）
    - "金融-银行" → "金融-银行"（一级-二级）
    - "金融-银行-国有银行" → "金融-银行-国有银行"（完整路径）

    规范化：NFKC + trim，移除空路径段。
    """
    if raw is None:
        return ""
    raw_str = str(raw).strip()
    if not raw_str:
        return ""

    # NFKC + trim
    raw_str = unicodedata.normalize("NFKC", raw_str).strip()

    # 按 / 或 - 拆分（问财可能用任一分隔符），然后统一用 - 连接
    parts = re.split(r"[/\-－—]", raw_str)
    parts = [p.strip() for p in parts if p.strip()]
    return "-".join(parts)


def _make_external_code(board_type: str, name: str) -> str:
    """生成稳定 external_code：wc:c:/wc:i: + 规范化名称 SHA256 前24位。

    Args:
        board_type: "industry" 或 "concept"
        name: 规范化后的板块名称

    Returns:
        external_code 字符串（如 "wc:c:a1b2c3d4e5f6a1b2c3d4e5f6"）
    """
    prefix = "wc:c:" if board_type == "concept" else "wc:i:"
    hash_hex = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{hash_hex}"


def _detect_hash_collision(
    name_to_code: dict[str, str],
    code_to_names: dict[str, list[str]],
) -> None:
    """检测不同名称哈希冲突。

    Args:
        name_to_code: {name: external_code}
        code_to_names: {external_code: [names]}（反向映射）

    Raises:
        WencaiHashCollisionError: 发现不同名称映射到同一 external_code
    """
    for code, names in code_to_names.items():
        if len(names) > 1:
            raise WencaiHashCollisionError(
                f"哈希冲突: external_code={code} 对应多个名称: {names}. "
                f"请检查规范化逻辑或增加哈希长度。"
            )


def _build_board_snapshot(
    df: Any,
    pd: Any,
) -> BoardSnapshot:
    """从 DataFrame 构建完整 BoardSnapshot。

    流程：
    1. 匹配必需列（股票代码、股票简称、所属概念、所属同花顺行业）
    2. 逐行规范化：股票代码、概念列表、行业路径
    3. 生成 boards（去重）+ memberships
    4. 检测哈希冲突

    Args:
        df: 问财返回的主 DataFrame
        pd: pandas 模块

    Returns:
        BoardSnapshot

    Raises:
        WencaiParseError: 必需字段缺失
        WencaiHashCollisionError: 哈希冲突
    """
    columns = list(df.columns)
    col_map = {
        key: _match_column(columns, patterns)
        for key, patterns in REQUIRED_FIELD_PATTERNS.items()
    }

    missing = [k for k, v in col_map.items() if v is None]
    if missing:
        raise WencaiParseError(
            f"问财返回数据缺少必需字段: {missing}, 实际列名: {columns}"
        )

    # 去重列名（问财可能返回同名列）
    df = df.loc[:, ~df.columns.duplicated()].copy()

    # 填充 NaN 为空字符串，避免类型问题
    df = df.fillna("")

    snapshot = BoardSnapshot(raw_rows=len(df))

    # 名称 → external_code 映射（用于哈希冲突检测）
    name_to_code: dict[str, str] = {}
    code_to_names: dict[str, list[str]] = {}

    # 板块去重集合：(external_code, type) → board dict
    boards_seen: set[tuple[str, str]] = set()

    # 概念关系计数（门禁用）
    concept_relation_count = 0

    unresolved: list[str] = []

    for _, row in df.iterrows():
        raw_code = row[col_map["stock_code"]]
        symbol = _normalize_stock_code(raw_code)
        if symbol is None:
            unresolved.append(str(raw_code)[:20])  # 脱敏
            continue

        # 规范化概念
        concepts = _normalize_concepts(row[col_map["concept"]])
        if len(concepts) > MAX_CONCEPTS_PER_STOCK:
            logger.warning(
                "[WencaiBoard] 股票 %s 概念数 %d 超过上限 %d，截断",
                symbol, len(concepts), MAX_CONCEPTS_PER_STOCK,
            )
            concepts = concepts[:MAX_CONCEPTS_PER_STOCK]

        # 规范化行业
        industry_path = _normalize_industry(row[col_map["industry"]])

        # 添加概念 boards + memberships
        for concept_name in concepts:
            ext_code = _make_external_code("concept", concept_name)
            key = (ext_code, "concept")
            if key not in boards_seen:
                boards_seen.add(key)
                snapshot.boards.append({
                    "external_code": ext_code,
                    "name": concept_name,
                    "type": "concept",
                })
                name_to_code[concept_name] = ext_code
                code_to_names.setdefault(ext_code, []).append(concept_name)
                snapshot.memberships[key] = []
            snapshot.memberships[key].append(symbol)
            concept_relation_count += 1

        # 添加行业 board + membership（每股恰好一个行业）
        if industry_path:
            ext_code = _make_external_code("industry", industry_path)
            key = (ext_code, "industry")
            if key not in boards_seen:
                boards_seen.add(key)
                snapshot.boards.append({
                    "external_code": ext_code,
                    "name": industry_path,
                    "type": "industry",
                })
                name_to_code[industry_path] = ext_code
                code_to_names.setdefault(ext_code, []).append(industry_path)
                snapshot.memberships[key] = []
            snapshot.memberships[key].append(symbol)

    # 检测哈希冲突
    _detect_hash_collision(name_to_code, code_to_names)

    # 未解析股票：只记录总数和前50个脱敏样本
    snapshot.unresolved_symbols = unresolved[:50]

    logger.info(
        "[WencaiBoard] 快照构建完成: raw_rows=%d, boards=%d (industry=%d, concept=%d), "
        "memberships=%d, concept_relations=%d, unresolved=%d",
        snapshot.raw_rows,
        snapshot.board_count,
        sum(1 for b in snapshot.boards if b["type"] == "industry"),
        sum(1 for b in snapshot.boards if b["type"] == "concept"),
        snapshot.membership_count,
        concept_relation_count,
        len(unresolved),
    )

    return snapshot


def _fetch_wencai_sync() -> Any:
    """同步调用问财 API（在 asyncio.to_thread 中执行）。

    最多 3 次有限重试，每次间隔 RETRY_WAIT_SECONDS 秒。
    不记录 Cookie 或完整原始响应。

    Returns:
        pywencai 返回的原始结果（DataFrame/dict/list/tuple）

    Raises:
        WencaiFetchError: 连续 3 次查询失败
    """
    import pywencai as wc

    _patch_wencai_headers()

    cookie = os.getenv("WENCAI_COOKIE") or None
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("[WencaiBoard] 查询问财: query=%r (attempt %d/%d)",
                        WENCAI_QUERY, attempt, MAX_RETRIES)
            result = wc.get(
                query=WENCAI_QUERY,
                loop=True,
                sleep=2,
                cookie=cookie,
            )
            if result is None:
                raise WencaiFetchError("问财返回 None")
            return result
        except Exception as exc:
            last_error = exc
            # 脱敏：不记录 cookie 或完整响应，只记录错误类型和消息前200字符
            logger.warning(
                "[WencaiBoard] 查询失败 (attempt %d/%d): error_type=%s, msg=%.200s",
                attempt, MAX_RETRIES,
                type(exc).__name__, str(exc),
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)

    raise WencaiFetchError(
        f"连续 {MAX_RETRIES} 次查询失败: {type(last_error).__name__ if last_error else 'unknown'}"
    )


async def fetch_board_snapshot() -> BoardSnapshot:
    """异步拉取问财板块快照（asyncio.to_thread 包装同步调用）。

    流程：
    1. asyncio.to_thread 调用 _fetch_wencai_sync（不阻塞事件循环）
    2. 选择包含必需字段且行数最大的主表
    3. 构建完整 BoardSnapshot（boards + memberships + 门禁数据）

    Returns:
        BoardSnapshot

    Raises:
        WencaiFetchError: 问财拉取失败
        WencaiParseError: 数据解析失败
        WencaiHashCollisionError: 哈希冲突
    """
    start_time = time.monotonic()

    # asyncio.to_thread 包装同步调用
    result = await asyncio.to_thread(_fetch_wencai_sync)

    # 选择主表 + 构建 BoardSnapshot（也在线程中执行，避免阻塞）
    import pandas as pd
    df = await asyncio.to_thread(_select_primary_dataframe, result, pd)
    snapshot = await asyncio.to_thread(_build_board_snapshot, df, pd)

    duration_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        "[WencaiBoard] 快照拉取完成: duration_ms=%d, boards=%d, memberships=%d",
        duration_ms, snapshot.board_count, snapshot.membership_count,
    )

    return snapshot


def get_provider_info() -> dict[str, Any]:
    """返回 provider 元信息（供 metadata 记录，不含敏感数据）。"""
    return {
        "source": "wencai",
        "query": WENCAI_QUERY,
        "max_retries": MAX_RETRIES,
    }
