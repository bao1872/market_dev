"""板块筛选共享 EXISTS 条件构造器。

抽取自 market_stocks_service._build_board_filter_conditions，供：
- market_stocks_service.get_market_stocks（以 Instrument.id 为关联列）
- strategy_result_repository._apply_run_item_filters（以 StrategyRunItem.instrument_id 为关联列）

共用同一份 EXISTS 子查询，保证两条查询链的行业/概念筛选语义一致。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, select

from app.models.market_board import MarketBoard, MarketBoardMembership


def build_board_filter_conditions(
    # SQLAlchemy InstrumentedAttribute[UUID] 与 ColumnElement[Any] 存在泛型协变限制，
    # 此处接受任意列表达式（Instrument.id / StrategyRunItem.instrument_id 均可）
    instrument_id_col: Any,
    industry: str | None,
    concept: str | None,
) -> list[ColumnElement[bool]]:
    """构建行业/概念筛选 EXISTS 条件（PRD §7.5）。

    使用 SQL EXISTS 子查询，避免先加载全量 UUID 再 IN 的 N+1 模式。
    industry/concept 参数为板块名称，通过 market_boards.name 匹配。
    industry+concept 同时提供时为 AND 语义（同时属于该行业和该概念）。

    Args:
        instrument_id_col: 外层查询的 instrument_id 列表达式
            (Instrument.id 或 StrategyRunItem.instrument_id)
        industry: 行业板块名称（可选）
        concept: 概念板块名称（可选）

    Returns:
        EXISTS 条件列表；为空表示无板块筛选。
    """
    conditions: list[ColumnElement[bool]] = []
    if industry:
        industry_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == instrument_id_col,
                MarketBoard.type == "industry",
                MarketBoard.name == industry,
            )
            .exists()
        )
        conditions.append(industry_exists)
    if concept:
        concept_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == instrument_id_col,
                MarketBoard.type == "concept",
                MarketBoard.name == concept,
            )
            .exists()
        )
        conditions.append(concept_exists)
    return conditions
