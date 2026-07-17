"""板块筛选共享 EXISTS 条件构造器。

抽取自 market_stocks_service._build_board_filter_conditions，供：
- market_stocks_service.get_market_stocks（以 Instrument.id 为关联列）
- strategy_result_repository._apply_run_item_filters（以 StrategyRunItem.instrument_id 为关联列）

共用同一份 EXISTS 子查询，保证两条查询链的行业/概念筛选语义一致。

[CHANGE-20260716-007] industry 语义改为"行业关键词"：
- 数据库存储完整路径 "一级-二级-三级"（保持不变）
- 筛选使用 ilike 关键词包含匹配，可命中完整路径中的任意一级
- 输入 "电子" / "半导体" / "数字芯片" 均应匹配所有路径中包含该词的股票
- concept 暂保持精确匹配（明确概念）
- industry+concept 同时提供时为 AND 语义
"""

from __future__ import annotations

import unicodedata
from typing import Any

from sqlalchemy import ColumnElement, select

from app.models.market_board import MarketBoard, MarketBoardMembership

# ilike 转义字符（PostgreSQL/SQLite 均支持 escape 子句）
_ILIKE_ESCAPE_CHAR = "\\"


def _normalize_keyword(raw: str) -> str:
    """规范化行业关键词：NFKC + trim。

    - NFKC：兼容性分解后重组，全角字符规范化为半角（如 "Ａ" → "A"）
    - strip：去除首尾空白
    - 空串返回空串（调用方据此跳过条件生成）
    """
    if not raw:
        return ""
    return unicodedata.normalize("NFKC", raw).strip()


def _escape_ilike_pattern(keyword: str) -> str:
    """转义 ilike 模式中的特殊字符。

    PostgreSQL ilike 模式中的元字符：
    - `%`：匹配任意长度字符序列
    - `_`：匹配任意单个字符
    - `\\`：转义字符本身

    使用 escape='\\' 子句时，需要将 `\\` / `%` / `_` 前各加一个 `\\` 转义，
    使其按字面字符匹配。

    Args:
        keyword: 规范化后的关键词（非空）

    Returns:
        转义后的安全 pattern 片段（不含外层 `%`）
    """
    # 顺序很重要：先转义反斜杠自身，再转义 % 和 _
    escaped = keyword.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%")
    escaped = escaped.replace("_", "\\_")
    return escaped


def build_board_filter_conditions(
    # SQLAlchemy InstrumentedAttribute[UUID] 与 ColumnElement[Any] 存在泛型协变限制，
    # 此处接受任意列表达式（Instrument.id / StrategyRunItem.instrument_id 均可）
    instrument_id_col: Any,
    industry: str | None,
    concept: str | None,
) -> list[ColumnElement[bool]]:
    """构建行业/概念筛选 EXISTS 条件（PRD §7.5）。

    使用 SQL EXISTS 子查询，避免先加载全量 UUID 再 IN 的 N+1 模式。

    industry 语义（CHANGE-20260716-007）：行业关键词
    - 规范化：NFKC + trim
    - 匹配：ilike '%keyword%' escape='\\'，命中完整路径中任意一级
    - 空值/纯空白：不生成条件
    - URL/preset/导出字段名仍为 industry（兼容现有链接），值现在是关键词

    concept 语义：精确概念名称（NFKC + trim 规范化后精确匹配，PR #77 收口）

    industry+concept 同时提供时为 AND 语义（同时属于该行业和该概念）。

    Args:
        instrument_id_col: 外层查询的 instrument_id 列表达式
            (Instrument.id 或 StrategyRunItem.instrument_id)
        industry: 行业关键词（可选，规范化后做 ilike 包含匹配）
        concept: 概念板块名称（可选，精确匹配）

    Returns:
        EXISTS 条件列表；为空表示无板块筛选。
    """
    conditions: list[ColumnElement[bool]] = []

    # 行业：规范化后非空才生成 ilike 关键词条件
    industry_keyword = _normalize_keyword(industry or "")
    if industry_keyword:
        escaped = _escape_ilike_pattern(industry_keyword)
        # 使用 ilike 包含匹配：完整路径 "一级-二级-三级" 中任意位置出现 keyword 即命中
        # 例：keyword="半导体" 命中 "电子-半导体-数字芯片"
        #     keyword="电子"     命中 "电子-半导体-数字芯片"
        #     keyword="数字芯片" 命中 "电子-半导体-数字芯片"
        pattern = f"%{escaped}%"
        industry_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == instrument_id_col,
                MarketBoard.type == "industry",
                MarketBoard.name.ilike(pattern, escape=_ILIKE_ESCAPE_CHAR),
            )
            .exists()
        )
        conditions.append(industry_exists)

    # 概念：规范化后非空才生成精确匹配条件（PR #77 收口：NFKC + trim）
    concept_name = _normalize_keyword(concept or "")
    if concept_name:
        concept_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == instrument_id_col,
                MarketBoard.type == "concept",
                MarketBoard.name == concept_name,
            )
            .exists()
        )
        conditions.append(concept_exists)
    return conditions
