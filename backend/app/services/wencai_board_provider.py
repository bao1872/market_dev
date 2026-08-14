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

# provider 整体超时（含重试与 to_thread 包装），防止 io 阻塞拖垮事件循环。
# 注：[PRD Alignment Pass P1-1] PRD instruction.md 未强制具体秒数；
# worker 实测单次 pywencai.get() 约 259s，盘后同步曾因 120s 超时被杀导致失败；
# 调整为 600s（10 分钟）以容纳真实拉取耗时，与 PRD 无数值冲突。
# 若后续 PRD 明确外层总超时阈值，应在此对齐并记录 Change。
PROVIDER_TIMEOUT_SECONDS = 600

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

# 行业层级上限（L1/L2/L3）
MAX_INDUSTRY_DEPTH = 3

# ===== 板块分类学/身份合同版本（单一来源，禁止使用 qstock 默认值）=====
# 所有 wencai board 必须显式携带 taxonomy/source/taxonomy_version/
# taxonomy_compatibility_key/identity_contract_version，禁止在消费端回退 qstock 默认值。
BOARD_TAXONOMY = "wencai"
BOARD_SOURCE = "wencai"
BOARD_TAXONOMY_VERSION = "wencai-hierarchy-v1"
BOARD_TAXONOMY_COMPATIBILITY_KEY = "wencai-board-v1"
BOARD_IDENTITY_CONTRACT_VERSION = "wencai-identity-v1"
# provider 合同版本（fetch 层），供 BoardFactsRun.provider_contract_version 记录
BOARD_PROVIDER_CONTRACT_VERSION = "wencai-provider-v1"
# 规范化合同版本，供 BoardFactsRun.normalization_contract_version 记录
BOARD_NORMALIZATION_CONTRACT_VERSION = "wencai-normalization-v1"
# 质量门禁版本，供 BoardFactsRun.quality_gate_version 记录
BOARD_QUALITY_GATE_VERSION = "board-gate-v1"


class WencaiBoardProviderError(Exception):
    """问财板块数据源错误基类。"""


class WencaiFetchError(WencaiBoardProviderError):
    """问财拉取失败（网络/接口/重试耗尽）。"""


class WencaiParseError(WencaiBoardProviderError):
    """问财返回数据解析失败（字段缺失/格式异常）。"""


class WencaiHashCollisionError(WencaiBoardProviderError):
    """不同名称哈希冲突（理论极低概率，发现立即失败）。"""


class WencaiConceptLimitError(WencaiBoardProviderError):
    """单股概念数超过上限（PRD 硬编码 100），禁止静默截断，必须门禁失败。"""


class WencaiIndustryDepthError(WencaiBoardProviderError):
    """行业层级深度超过三级（PRD 硬性上限），视为格式异常，禁止静默截断。"""


@dataclass
class BoardSnapshot:
    """完整的板块快照（内存中构造，供原子切换）。

    Attributes:
        boards: [{external_code, name, type}]，type ∈ {"industry", "concept"}
        memberships: {(external_code, type): [symbol, ...]}
        raw_rows: 原始行数（门禁用）
        unresolved_symbols: 未解析为有效 A 股代码的原始值（脱敏样本，前50个）
        diagnostics: 结构化诊断（主表选择、字段匹配、耗时等），供 metadata 记录
    """

    boards: list[dict[str, str]] = field(default_factory=list)
    memberships: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    raw_rows: int = 0
    unresolved_symbols: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

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


def _match_required_columns(columns: list[str]) -> dict[str, str | None]:
    """匹配 DataFrame 列名中的必需四类字段。

    Returns:
        {field_key: 匹配到的列名}，缺失字段的值为 None
    """
    return {
        key: _match_column(columns, patterns)
        for key, patterns in REQUIRED_FIELD_PATTERNS.items()
    }


def _df_cell_str(value: Any) -> str:
    """将单元格值规范化为稳定字符串（None/NaN → 空串）。"""
    if value is None:
        return ""
    try:
        # float('nan') 不自等，视为缺失
        if isinstance(value, float) and value != value:
            return ""
    except Exception:  # pragma: no cover - 防御性兜底
        pass
    return str(value)


def _df_content_hash(df: Any) -> str:
    """对 DataFrame 内容计算稳定 hash（用于多表哈希冲突检测）。

    基于规范化后的四类必需字段，按**整行 tuple** 排序后拼接 hash：
    1. 同一批数据即使行序不同也得到相同 hash（顺序无关）
    2. 保留整行内字段间关系（股票与行业/概念的对应关系不被拆散）

    禁止把不同列值拆散排序——那会丢失"某行股票对应某个概念/行业"的行关系。
    """
    columns = list(df.columns)
    col_map = _match_required_columns(columns)
    rows: list[tuple[str, str, str, str]] = []
    for _, row in df.iterrows():
        rows.append((
            _df_cell_str(row[col_map["stock_code"]]) if col_map["stock_code"] else "",
            _df_cell_str(row[col_map["stock_name"]]) if col_map["stock_name"] else "",
            _df_cell_str(row[col_map["concept"]]) if col_map["concept"] else "",
            _df_cell_str(row[col_map["industry"]]) if col_map["industry"] else "",
        ))
    # 对整行 tuple 排序，保证行序无关同时保留字段间关系
    rows.sort()
    material = "\n".join(f"{sc}|{sn}|{c}|{i}" for sc, sn, c, i in rows)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _select_primary_dataframe(result: Any, pd: Any) -> Any:
    """从问财返回值中选择包含必需字段且行数最大的主表。

    选择逻辑（[Commit A §6.2] 严格选择，禁止缺列后静默降级）：
    1. 收集所有嵌套 DataFrame
    2. 过滤出包含全部必需字段的合格表；若无合格表 → 直接失败（不降级到最大表）
    3. 若存在多张合格表且行数相同但内容 hash 冲突 → 失败
    4. 否则按行数×列数降序选择最大合格表

    Raises:
        WencaiParseError: 无 DataFrame 或无合格表
        WencaiHashCollisionError: 同等合格表内容 hash 冲突
    """
    frames = _collect_dataframes(result, pd)
    if not frames:
        raise WencaiParseError("问财未返回可保存的表格数据")

    # 合格表：包含全部必需字段
    qualified: list[tuple[str, Any, dict[str, str | None]]] = []
    for label, df in frames:
        col_map = _match_required_columns(list(df.columns))
        if all(v is not None for v in col_map.values()):
            qualified.append((label, df, col_map))

    if not qualified:
        columns_sample = list(frames[0][1].columns)
        raise WencaiParseError(
            "问财返回的表格均缺少必需字段，禁止静默降级到最大表，"
            f"共 {len(frames)} 张表，样本列名: {columns_sample}"
        )

    # 多合格表且行数相同但内容 hash 冲突 → 失败
    max_rows = max(len(df) for _, df, _ in qualified)
    same_size = [(label, df) for label, df, _ in qualified if len(df) == max_rows]
    if len(same_size) > 1:
        hashes = {_df_content_hash(df) for _, df in same_size}
        if len(hashes) > 1:
            raise WencaiHashCollisionError(
                f"多张同等合格（{max_rows} 行）且行数最大的表内容 hash 冲突，"
                f"无法确定唯一主表，labels={[label for label, _ in same_size]}"
            )

    # 按行数×列数降序，选择最大合格表
    qualified.sort(key=lambda item: (len(item[1]), len(item[1].columns)), reverse=True)
    selected_label, selected_df, selected_col_map = qualified[0]
    logger.info(
        "[WencaiBoard] 选择主表: label=%s, rows=%d, cols=%d, 合格表数=%d",
        selected_label, len(selected_df), len(selected_df.columns), len(qualified),
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


def _split_industry_path(path: str) -> list[str]:
    """把规范化行业路径拆分为有序层级段（L1/L2/L3）。

    深度超过 MAX_INDUSTRY_DEPTH（三级）视为格式异常，抛 WencaiIndustryDepthError，
    禁止静默截断（PRD 硬性上限；深度超限进入 invalid depth 统计与门禁）。

    例：
        "银行"                 → ["银行"]
        "金融-银行"             → ["金融", "银行"]
        "金融-银行-国有银行"      → ["金融", "银行", "国有银行"]
        "金融-银行-国有银行-细分" → WencaiIndustryDepthError

    Args:
        path: 规范化行业路径（"-" 连接，可能为空）

    Returns:
        有序层级段列表

    Raises:
        WencaiIndustryDepthError: 行业深度超过三级
    """
    if not path:
        return []
    parts = [p.strip() for p in path.split("-") if p.strip()]
    if not parts:
        return []
    if len(parts) > MAX_INDUSTRY_DEPTH:
        raise WencaiIndustryDepthError(
            f"行业路径深度 {len(parts)} 超过三级上限: '{path}'"
        )
    return parts


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
    snapshot.diagnostics["required_column_mapping"] = dict(col_map)

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
            # [PRD Alignment Pass P0-3] 禁止静默截断；超过上限必须门禁失败
            raise WencaiConceptLimitError(
                f"股票 {symbol} 概念数 {len(concepts)} 超过上限 "
                f"{MAX_CONCEPTS_PER_STOCK}，禁止截断"
            )

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
                    # [Commit A 2026-08-05] 显式分类学/身份合同，禁止下游回退 qstock 默认值
                    "taxonomy": BOARD_TAXONOMY,
                    "source": BOARD_SOURCE,
                    "taxonomy_version": BOARD_TAXONOMY_VERSION,
                    "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
                    "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
                })
                name_to_code[concept_name] = ext_code
                code_to_names.setdefault(ext_code, []).append(concept_name)
                snapshot.memberships[key] = []
            snapshot.memberships[key].append(symbol)
            concept_relation_count += 1

        # 添加行业 L1/L2/L3 层级 boards + membership
        # [Commit A §6.2] 每股所属行业拆分为有序层级，股票挂到每一级 board，
        # 层级间通过 parent_external_code 建立父子身份（L1 → L2 → L3）。
        if industry_path:
            levels = _split_industry_path(industry_path)
            parent_code: str | None = None
            for level_idx, _level_name in enumerate(levels):
                # 从 L1 到当前层级的完整名称（"金融" → "金融-银行" → "金融-银行-国有银行"）
                level_path = "-".join(levels[: level_idx + 1])
                hierarchy_level = f"L{level_idx + 1}"
                ext_code = _make_external_code("industry", level_path)
                key = (ext_code, "industry")
                if key not in boards_seen:
                    boards_seen.add(key)
                    board: dict[str, str] = {
                        "external_code": ext_code,
                        "name": level_path,
                        "type": "industry",
                        "hierarchy_level": hierarchy_level,
                        # [Commit A 2026-08-05] 显式分类学/身份合同，禁止下游回退 qstock 默认值
                        "taxonomy": BOARD_TAXONOMY,
                        "source": BOARD_SOURCE,
                        "taxonomy_version": BOARD_TAXONOMY_VERSION,
                        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
                        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
                    }
                    # parent_external_code 仅 L2/L3 存在（L1 为 None，省略以保持 dict[str, str] 类型）
                    if parent_code is not None:
                        board["parent_external_code"] = parent_code
                    snapshot.boards.append(board)
                    name_to_code[level_path] = ext_code
                    code_to_names.setdefault(ext_code, []).append(level_path)
                    snapshot.memberships[key] = []
                snapshot.memberships[key].append(symbol)
                parent_code = ext_code

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


_FETCH_SUBPROCESS_POLL_SECONDS = 0.2

# 子进程终止时优雅等待，超过仍存活则升级为 kill
TERMINATE_JOIN_TIMEOUT = 5
KILL_JOIN_TIMEOUT = 2


def _subprocess_fetch_worker(queue: Any) -> None:
    """在独立子进程中执行 pywencai 拉取，把结果/错误放入 multiprocessing Queue。

    作为 scripts 顶层函数，可被 spawn 子进程 picklable 序列化。
    若结果不可 pickle（queue.put 抛 PicklingError），同样回传结构化错误，
    避免父进程只能靠超时兜底。
    """
    try:
        result = _fetch_wencai_sync()
        queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001 - 边界捕获，脱敏后回传
        queue.put(("error", type(exc).__name__, str(exc)[:500]))


def _terminate_subprocess(proc: Any) -> None:
    """终止子进程：先 terminate，仍存活则 kill，并等待退出。幂等。"""
    if getattr(proc, "pid", None) is None:
        return
    if not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=TERMINATE_JOIN_TIMEOUT)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=KILL_JOIN_TIMEOUT)


async def _fetch_with_terminable_subprocess(ctx: Any | None = None) -> Any:
    """在可终止子进程中执行 pywencai 拉取，超时后真正终止底层调用。

    [Commit A 修正 2026-08-05] 原实现用 asyncio.wait_for(asyncio.to_thread(...))
    包装 _fetch_wencai_sync：超时会取消等待，但底层 to_thread 线程仍会继续运行
    pywencai（不可终止），多次超时会导致后台线程叠加。此处改为在独立
    spawn 子进程中执行，父进程在 PROVIDER_TIMEOUT_SECONDS 内非阻塞轮询
    multiprocessing Queue；超时则真正终止子进程（含 pywencai 网络调用），
    杜绝线程/进程叠加。

    [Corrective-2 2026-08-05] 子进程生命周期加固：
    1. 轮询仅捕获 queue.Empty（不再吞掉其他异常）
    2. 子进程已退出但队列仍空 → 识别为异常终止（如不可 pickle 结果未入队）
    3. terminate 后仍存活则 kill
    4. 队列 close/join_thread 释放 feeder 线程资源

    Args:
        ctx: multiprocessing 上下文（测试注入用；默认 spawn）

    Returns:
        pywencai 返回的原始结果（可由 Queue 传输，DataFrame/dict/list/tuple）

    Raises:
        WencaiFetchError: 超时（子进程已被终止）、子进程异常退出或拉取失败
    """
    import multiprocessing as mp
    from queue import Empty as _QueueEmpty

    mp_ctx = ctx or mp.get_context("spawn")
    queue = mp_ctx.Queue()
    proc = mp_ctx.Process(target=_subprocess_fetch_worker, args=(queue,), daemon=True)

    start = time.monotonic()
    proc.start()
    deadline = start + PROVIDER_TIMEOUT_SECONDS
    item: tuple[Any, ...] | None = None
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            try:
                item = queue.get_nowait()
                break
            except _QueueEmpty:
                # 子进程已退出但队列仍空 → 异常终止（如不可 pickle 结果未入队）
                if not proc.is_alive() and proc.exitcode not in (None, 0):
                    raise WencaiFetchError(
                        f"问财子进程异常退出（exitcode={proc.exitcode}），未返回结果"
                    ) from None
                await asyncio.sleep(_FETCH_SUBPROCESS_POLL_SECONDS)
    except (TimeoutError, WencaiFetchError) as exc:
        # 真正终止子进程（含底层 pywencai 网络调用），并等待其退出
        _terminate_subprocess(proc)
        if isinstance(exc, TimeoutError):
            raise WencaiFetchError(
                f"问财拉取超时（>{PROVIDER_TIMEOUT_SECONDS}s），子进程已终止"
            ) from exc
        raise
    finally:
        # 兜底：先终止子进程，避免其在队列关闭后仍尝试写入/阻塞 feeder 线程
        _terminate_subprocess(proc)
        # 队列资源释放：close + join feeder 线程，避免后台线程残留
        queue.close()
        queue.join_thread()

    if item is None:
        raise WencaiFetchError("问财拉取未返回结果")
    if item[0] == "error":
        raise WencaiFetchError(f"问财拉取失败: {item[1]}: {item[2]}")
    return item[1]


async def fetch_board_snapshot() -> BoardSnapshot:
    """异步拉取问财板块快照（pywencai 在可终止子进程中执行）。

    流程：
    1. 在可终止子进程中执行 pywencai 拉取（超时真正终止，防止线程叠加）
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

    # 在可终止子进程中执行 pywencai（超时真正终止，杜绝 to_thread 线程叠加）
    result = await _fetch_with_terminable_subprocess()

    # 选择主表 + 构建 BoardSnapshot（纯 CPU 函数，耗时有限，用 to_thread 包装避免阻塞）
    import pandas as pd
    df = await asyncio.to_thread(_select_primary_dataframe, result, pd)
    snapshot = await asyncio.to_thread(_build_board_snapshot, df, pd)

    duration_ms = int((time.monotonic() - start_time) * 1000)
    snapshot.diagnostics.update({
        "duration_ms": duration_ms,
        "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
        # [Commit A 2026-08-05] 合同版本单一来源，供 BoardFactsRun 记录
        "provider_contract_version": BOARD_PROVIDER_CONTRACT_VERSION,
        "normalization_contract_version": BOARD_NORMALIZATION_CONTRACT_VERSION,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "quality_gate_version": BOARD_QUALITY_GATE_VERSION,
    })
    logger.info(
        "[WencaiBoard] 快照拉取完成: duration_ms=%d, boards=%d, memberships=%d",
        duration_ms, snapshot.board_count, snapshot.membership_count,
    )

    return snapshot


def get_provider_info() -> dict[str, Any]:
    """返回 provider 元信息（供 metadata 记录，不含敏感数据）。"""
    return {
        "source": BOARD_SOURCE,
        "query": WENCAI_QUERY,
        "max_retries": MAX_RETRIES,
        "timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
        "industry_max_depth": MAX_INDUSTRY_DEPTH,
        "taxonomy": BOARD_TAXONOMY,
        "taxonomy_version": BOARD_TAXONOMY_VERSION,
        "taxonomy_compatibility_key": BOARD_TAXONOMY_COMPATIBILITY_KEY,
        "identity_contract_version": BOARD_IDENTITY_CONTRACT_VERSION,
        "provider_contract_version": BOARD_PROVIDER_CONTRACT_VERSION,
        "normalization_contract_version": BOARD_NORMALIZATION_CONTRACT_VERSION,
        "quality_gate_version": BOARD_QUALITY_GATE_VERSION,
    }
