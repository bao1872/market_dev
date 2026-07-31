"""行情列表查询服务 - 批量 JOIN 查询，禁止 N+1。

对应 PRD §8.1 行情列表契约 + §9.2 后端改造指引：
- 市场查询在 repository/service 层一次 join/批量加载，禁止 API 层循环调用单股服务。
- 每行一次返回页面所需全部字段。

查询策略（固定 SQL 数量，无逐行查询）：
1. instruments + is_watchlisted + 分页（scope=market 用 EXISTS，scope=watchlist 用 INNER JOIN）
2. count 查询（相同 WHERE 条件，含 industry/concept EXISTS 子查询）
3. 最新 2 根日线（rn <= 2）批量按 instrument_ids 查询 → latest_price + change_pct
4. 最新 stock_feature_snapshot（rn = 1）批量 → dsa_state + structure_state
5. 板块归属批量查询 → industry + concepts
6. boards_as_of — MAX(market_boards.updated_at) 标量查询
7. price_as_of — MAX(bar_daily.trade_date) 全局标量（不随分页变化）
8. state_as_of — MAX(stock_feature_snapshot.created_at) 全局标量（不随分页变化）

总计 8 条固定 SQL，不随 page_size 增长。

注：latest_event_title / latest_event_time 字段为兼容保留，固定返回 None；
列表服务不再执行 stock_state_event 批量查询（事件只在 EventStatePanel 按需展开时加载）。
sort=latest_event_time 仍可用（通过标量子查询 ORDER BY，非批量查询）。
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
import typing as _typing
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Float,
    Integer,
    Text,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import to_shanghai_iso
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_state_event import StockStateEvent
from app.models.strategy_run import StrategyResult, StrategyRun
from app.models.watchlist import UserWatchlistItem
from app.repositories.board_filter_helper import build_board_filter_conditions
from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION
from app.schemas.market_stocks import (
    MarketBoardItem,
    MarketBoardsResponse,
    MarketStockRow,
    MarketStocksResponse,
)
from app.services.board_sync_service import get_instrument_boards_batch

# [CHANGE-20260718-007] - 使用生产者 _SCHEMA_VERSION 替代硬编码 == 1
# _SCHEMA_VERSION 从 1→2→3 升级后，生产写入 schema_version=3，
# 消费查询必须用同一常量，否则新快照读不到、as_of 永远为 None。
from app.services.feature_snapshot_service import _SCHEMA_VERSION
from app.services.first_pyramid_flatten import (
    FP_CHIP_KEYS,
    FP_QUERY_FIELD_SPECS,
    FpFilterSpec,
    FpSortSpec,
    flatten_first_pyramid,
)
from app.services.first_pyramid_flatten import (
    parse_fp_filter as _parse_fp_filter_impl,
)
from app.services.first_pyramid_flatten import (
    parse_fp_sort as _parse_fp_sort_impl,
)
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("market_stocks_service")

# 排序字段白名单（防止 SQL 注入）
_SORTABLE_FIELDS = {"name", "symbol", "change_pct", "dsa_state", "latest_event_time"}
_SORT_DIRECTIONS = {"asc", "desc"}

# [CHANGE-20260729-004 P0-1] fp_filter/fp_sort 操作符合法值（保留供校验引用）
# [CHANGE-20260730-013] 扩展操作符合同：neq/in/not_in/date_eq/before/after/has_any/has_all/not_has_any
_FP_FILTER_OPERATORS = {
    "contains", "not_contains", "eq", "neq", "gt", "gte", "lt", "lte",
    "between", "empty", "not_empty",
    "in", "not_in",
    "date_eq", "before", "after",
    "has_any", "has_all", "not_has_any",
}

# [CHANGE-20260729-009] DSA 策略 key（用于查询已发布 dsa_selector run 的 payload）
_DSA_STRATEGY_KEY = "dsa_selector"

# [CHANGE-20260729-009] 第一金字塔必选维度权威字段（任一非空即维度就绪）
_FP_TREND_KEYS = ("fp_trend_direction", "fp_trend_bars")
_FP_STRUCTURE_KEYS = ("fp_swing_direction", "fp_structure_alignment")
_FP_MOMENTUM_KEYS = ("fp_sqzmom_value", "fp_momentum_direction", "fp_squeeze_state")

# [CHANGE-20260729-009] chip 最低 15m bar 门槛（与 after_close_chip_consensus_service._CHIP_MIN_15M_BARS 一致）
_CHIP_MIN_15M_BARS = 500

# [CHANGE-20260729-009] 第一金字塔最低日线门槛（与 first_pyramid_service._MIN_BARS_FOR_REQUIRED_DIMS 一致）
_MIN_DAILY_BARS_FOR_FACTOR = 60


def _compute_factor_ready(
    flat_fp: dict[str, Any] | None,
    daily_bar_count: int | None = None,
) -> tuple[bool | None, str | None, int | None, int | None]:
    """判断第一金字塔必选维度是否就绪。

    Args:
        flat_fp: 扁平化的 first_pyramid dict（None 表示无快照或 first_pyramid 为 null）
        daily_bar_count: 该股票实际日线数（仅 flat_fp=None 时用于区分原因）

    Returns:
        (factor_ready, factor_error, actual_bars, required_bars)
        - flat_fp 为 None + daily_bars < 60: (False, "INSUFFICIENT_DAILY_BARS", actual, 60)
        - flat_fp 为 None + daily_bars >= 60: (False, "COMPUTE_FAILED", actual, 60)
        - flat_fp 为 None + daily_bars 未知: (False, "no_snapshot", None, None)
        - 任一维度权威字段全为 None: (False, "<dim>_missing", None, None)
        - 全部维度就绪: (True, None, None, None)
    """
    if flat_fp is None:
        if daily_bar_count is not None:
            if daily_bar_count < _MIN_DAILY_BARS_FOR_FACTOR:
                return False, "INSUFFICIENT_DAILY_BARS", daily_bar_count, _MIN_DAILY_BARS_FOR_FACTOR
            return False, "COMPUTE_FAILED", daily_bar_count, _MIN_DAILY_BARS_FOR_FACTOR
        return False, "no_snapshot", None, None

    def _any_non_none(keys: tuple[str, ...]) -> bool:
        return any(flat_fp.get(k) is not None for k in keys)

    if not _any_non_none(_FP_TREND_KEYS):
        return False, "trend_missing", None, None
    if not _any_non_none(_FP_STRUCTURE_KEYS):
        return False, "structure_missing", None, None
    if not _any_non_none(_FP_MOMENTUM_KEYS):
        return False, "momentum_missing", None, None
    return True, None, None, None


def _build_chip_status_struct(
    chip_row: Any | None,
) -> dict[str, Any] | None:
    """[CHANGE-20260730-010] 从 chip 记录构建结构化状态 dict（共享 chip_status_resolver）。

    Args:
        chip_row: 包含 status, chip_payload, error_message, created_at 的 NamedTuple，或 None

    Returns:
        camelCase ChipStatus dict（与 /first-pyramid 详情 API 完全一致）：
        {state, reasonCode, reasonText, computedAt, actualBars, requiredBars, fullQualityBars}
        或 None（chip_row=None 表示无 snap 关联，列表 API 返回 null）

    注意：旧版本返回 snake_case {status, reason_code, actual_bars, required_bars,
    reason_text, computed_at}，[CHANGE-20260730-010] 统一改为 camelCase，与详情 API 同口径。
    """
    if chip_row is None:
        return None

    # 延迟导入避免循环依赖（chip_status_resolver 不导入本模块的 _build_chip_status_struct）
    from app.services.chip_status_resolver import _build_chip_status_from_row

    chip_status = _build_chip_status_from_row(chip_row)
    return chip_status.model_dump(by_alias=False)


@dataclass(frozen=True)
class SortSpec:
    """排序规格：字段 + 方向。"""

    field: str
    direction: str


def _build_search_conditions(keyword: str | None) -> tuple[list[ColumnElement[bool]], ColumnElement[int]]:
    """构建搜索条件 + 命中优先级排序表达式。

    复用 instruments.py 的搜索逻辑：symbol 完全匹配 → symbol 前缀 → 拼音首字母前缀 → 名称包含。
    """
    conditions: list[ColumnElement[bool]] = [stock_symbol_sql_filter(Instrument)]
    rank_expr: ColumnElement[int] = literal(0)

    if keyword:
        keyword = unicodedata.normalize("NFKC", keyword)
        keyword_lower = keyword.lower()
        rank_expr = case(
            (Instrument.symbol == keyword, 0),
            (Instrument.symbol.ilike(f"{keyword}%"), 1),
            (Instrument.pinyin_initials.like(f"{keyword_lower}%"), 2),
            (Instrument.name.ilike(f"%{keyword}%"), 3),
            else_=4,
        )
        conditions.append(
            or_(
                Instrument.symbol == keyword,
                Instrument.symbol.ilike(f"{keyword}%"),
                Instrument.pinyin_initials.like(f"{keyword_lower}%"),
                Instrument.name.ilike(f"%{keyword}%"),
            )
        )

    return conditions, rank_expr


def _parse_sort(sort: str | None) -> SortSpec | None:
    """解析 sort=field:direction 参数，返回 SortSpec 或 None。

    支持字段：name, symbol, change_pct, dsa_state, latest_event_time。
    方向：asc, desc（默认 asc）。
    非法字段或方向抛出 ValueError（由 API 层转为 422）。
    """
    if not sort:
        return None

    parts = sort.split(":")
    field = parts[0].strip().lower()
    direction = parts[1].strip().lower() if len(parts) > 1 else "asc"

    if field not in _SORTABLE_FIELDS:
        raise ValueError(
            f"Invalid sort field '{field}'. Allowed: {', '.join(sorted(_SORTABLE_FIELDS))}"
        )
    if direction not in _SORT_DIRECTIONS:
        raise ValueError(f"Invalid sort direction '{direction}'. Allowed: asc, desc")

    return SortSpec(field=field, direction=direction)


def _build_sort_expression(field: str) -> ColumnElement:
    """构建排序标量表达式（用于 change_pct/dsa_state/latest_event_time）。

    name/symbol 直接使用 Instrument 列，不经过此函数。
    """
    if field == "change_pct":
        latest_close = (
            select(BarDaily.close)
            .where(BarDaily.instrument_id == Instrument.id)
            .order_by(BarDaily.trade_date.desc())
            .limit(1)
            .scalar_subquery()
        )
        prev_close = (
            select(BarDaily.close)
            .where(BarDaily.instrument_id == Instrument.id)
            .order_by(BarDaily.trade_date.desc())
            .offset(1)
            .limit(1)
            .scalar_subquery()
        )
        return case(
            (
                (prev_close.isnot(None)) & (prev_close != 0),
                (latest_close - prev_close) / prev_close * 100.0,
            ),
            else_=None,
        )

    if field == "dsa_state":
        return (
            select(
                cast(
                    StockFeatureSnapshot.summary_payload["daily_developing_swing_dir"].astext,
                    Integer,
                )
            )
            .where(StockFeatureSnapshot.instrument_id == Instrument.id)
            .order_by(StockFeatureSnapshot.trade_date.desc())
            .limit(1)
            .scalar_subquery()
        )

    if field == "latest_event_time":
        return (
            select(func.max(StockStateEvent.occurred_at))
            .where(StockStateEvent.instrument_id == Instrument.id)
            .scalar_subquery()
        )

    # 不应到达此处（_parse_sort 已校验白名单）
    raise ValueError(f"Unsupported sort field: {field}")


def _build_order_by(
    sort_spec: SortSpec | None,
    has_query: bool,
    rank_expr: ColumnElement[int],
    fp_sort_spec: FpSortSpec | None = None,
    snap_subq: Any | None = None,
    chip_subq: Any | None = None,
    max_trade_date_subq: Any | None = None,
) -> list[ColumnElement]:
    """构建 ORDER BY 列表。

    - 搜索模式（has_query=True）：按命中优先级排序，忽略 sort_spec/fp_sort_spec。
    - 无 sort_spec 且无 fp_sort_spec：默认 symbol asc。
    - name/symbol：直接使用 Instrument 列。
    - change_pct/dsa_state/latest_event_time：使用标量子查询表达式。
    - fp_sort（第一金字塔字段）：使用 LATERAL JOIN 列引用。
    - 所有非首序都追加 Instrument.symbol 作为第二排序键，NULLS LAST。
    """
    if has_query:
        return [rank_expr, Instrument.symbol.asc()]

    # fp_sort 优先于 sort（第一金字塔专用排序）
    if fp_sort_spec is not None:
        fp_sort_expr = _build_fp_sort_expression(
            fp_sort_spec, snap_subq, chip_subq, max_trade_date_subq,
        )
        order_col = (
            fp_sort_expr.desc().nullslast() if fp_sort_spec.direction == "desc"
            else fp_sort_expr.asc().nullslast()
        )
        return [order_col, Instrument.symbol.asc()]

    if sort_spec is None:
        return [Instrument.symbol.asc()]

    field = sort_spec.field
    direction = sort_spec.direction

    if field in ("name", "symbol"):
        col = getattr(Instrument, field)
        order_col = col.desc().nullslast() if direction == "desc" else col.asc().nullslast()
        return [order_col, Instrument.symbol.asc()]

    sort_expr = _build_sort_expression(field)
    order_col = (
        sort_expr.desc().nullslast() if direction == "desc" else sort_expr.asc().nullslast()
    )
    return [order_col, Instrument.symbol.asc()]


def _map_dsa_state(swing_dir: object) -> str | None:
    """将 daily_developing_swing_dir 映射为可读状态。"""
    if swing_dir is None:
        return None
    if isinstance(swing_dir, bool) or not isinstance(swing_dir, (int, float, str)):
        return None
    try:
        val = int(swing_dir)
    except (TypeError, ValueError):
        return None
    if val > 0:
        return "上行"
    if val < 0:
        return "下行"
    return "震荡"


def _build_state_filter(state: str | None) -> ColumnElement[bool] | None:
    """构建状态筛选条件（Phase 4 实现）。

    使用标量子查询取最新 snapshot 的 daily_developing_swing_dir，
    按状态码过滤：up → > 0, down → < 0, sideways → == 0。

    与 _build_sort_expression(dsa_state) 使用相同的子查询模式，
    确保 filter 和 sort 口径一致。
    """
    if state is None:
        return None

    swing_dir_subq = (
        select(
            cast(
                StockFeatureSnapshot.summary_payload["daily_developing_swing_dir"].astext,
                Integer,
            )
        )
        .where(StockFeatureSnapshot.instrument_id == Instrument.id)
        .order_by(StockFeatureSnapshot.trade_date.desc())
        .limit(1)
        .scalar_subquery()
    )

    if state == "up":
        return swing_dir_subq > 0
    if state == "down":
        return swing_dir_subq < 0
    if state == "sideways":
        return swing_dir_subq == 0
    return None


def _build_board_filter_conditions(
    industry: str | None, concept: str | None
) -> list[ColumnElement[bool]]:
    """构建行业/概念筛选 EXISTS 条件（委托共享 helper）。

    保留本地函数签名以兼容现有调用；实际逻辑由
    app.repositories.board_filter_helper.build_board_filter_conditions 提供，
    供 market_stocks_service 与 strategy_result_repository 共用同一份 EXISTS 子查询。
    """
    return build_board_filter_conditions(Instrument.id, industry, concept)


# =============================================================================
# [CHANGE-20260729-004 P0-1] 第一金字塔 fp_filter/fp_sort 解析与构建
# =============================================================================
# 解析逻辑已移至 first_pyramid_flatten 模块（纯函数，无 DB 依赖），
# 此处保留 thin wrapper 以兼容现有调用方（API 层 import _parse_fp_filter/_parse_fp_sort）。
# URL 编码格式：
#   fp_filter=key1:op1:val1[;val2];key2:op2:val2
#   fp_sort=key:direction
# 排序和筛选均在分页前完成；asc/desc 均 NULLS LAST，第二排序键固定为 Instrument.symbol
# =============================================================================

# FpFilterSpec / FpSortSpec 从 first_pyramid_flatten 导入（见文件顶部）


def _parse_fp_filter(fp_filter: str | None) -> list[FpFilterSpec]:
    """[CHANGE-20260729-005] 委托 first_pyramid_flatten.parse_fp_filter。"""
    return _parse_fp_filter_impl(fp_filter)


def _parse_fp_sort(fp_sort: str | None) -> FpSortSpec | None:
    """[CHANGE-20260729-005] 委托 first_pyramid_flatten.parse_fp_sort。"""
    return _parse_fp_sort_impl(fp_sort)


# =============================================================================
# [CHANGE-20260729-005 二.7] LATERAL JOIN：最新 snapshot + chip 单次关联
# =============================================================================

def _needs_snap_lateral(fp_filter_specs: list[FpFilterSpec], fp_sort_spec: FpSortSpec | None) -> bool:
    """判断是否需要 snapshot LATERAL JOIN（有 flat/column/computed source 字段参与筛选或排序）。"""
    keys = [f.fp_key for f in fp_filter_specs]
    if fp_sort_spec:
        keys.append(fp_sort_spec.fp_key)
    return any(
        FP_QUERY_FIELD_SPECS[k]["source"] in ("flat", "column", "computed")
        for k in keys
    )


def _needs_chip_lateral(fp_filter_specs: list[FpFilterSpec], fp_sort_spec: FpSortSpec | None) -> bool:
    """判断是否需要 chip LATERAL JOIN。

    [P0-2 修复] chip LATERAL 必须严格五元组匹配，且以下任一条件成立时才构建：
    - chip source 字段参与 filter/sort
    - fp_chip_available computed 字段参与 filter/sort（需 chip 存在性判定）
    """
    keys = [f.fp_key for f in fp_filter_specs]
    if fp_sort_spec:
        keys.append(fp_sort_spec.fp_key)
    for k in keys:
        spec = FP_QUERY_FIELD_SPECS[k]
        if spec["source"] == "chip":
            return True
        if (spec["source"] == "computed"
                and spec.get("computed_kind") == "chip_available"):
            return True
    return False


def _build_snap_lateral(*, snapshot_run_id: UUID | None = None):
    """构建最新 snapshot 的 LATERAL 子查询（每个 instrument 一行最新快照）。

    [CHANGE-20260729-008] 优先绑定 publication pointer.data_run_id：
    - snapshot_run_id 非 None：只读取该 run 的 snapshot（统一发布版本，禁止跨 run 混读）
    - snapshot_run_id 为 None：回退到每股 latest（仅在 DB 无 pointer 时兼容）

    Args:
        snapshot_run_id: 已发布 stock_core pointer.data_run_id（None 时回退 latest）
    """
    stmt = (
        select(
            StockFeatureSnapshot.id,
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.trade_date,
            StockFeatureSnapshot.source_run_id,
            StockFeatureSnapshot.created_at,
            StockFeatureSnapshot.summary_payload,
        )
        .where(
            StockFeatureSnapshot.instrument_id == Instrument.id,
            StockFeatureSnapshot.schema_version == _SCHEMA_VERSION,
        )
    )
    if snapshot_run_id is not None:
        # 严格绑定已发布 run：禁止跨 run 混读
        stmt = stmt.where(StockFeatureSnapshot.source_run_id == snapshot_run_id)
        # 同一 run 内每股仅一行，无需 order_by/limit；保留 limit(1) 防御重复
        stmt = stmt.limit(1)
    else:
        # 兼容回退：无 pointer 时按每股 latest
        stmt = stmt.order_by(StockFeatureSnapshot.trade_date.desc()).limit(1)
    return stmt.lateral("latest_snap")


def _build_chip_lateral(snap_subq: Any):
    """构建严格五元组匹配的 chip LATERAL 子查询。

    [P0-2 修复 2026-07-29] 禁止仅按股票取最新 chip。必须严格匹配：
        instrument_id == Instrument.id
        AND trade_date == latest_snap.trade_date
        AND core_run_id == latest_snap.source_run_id
        AND algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION
        AND status == 'succeeded'
    仅匹配最新 core 快照同交易日、同 run 的 succeeded chip，禁止挂旧 run chip。

    Args:
        snap_subq: latest_snap LATERAL 子查询（已加入 FROM 后可被引用）
    """
    return (
        select(
            StockChipConsensusSnapshot.id,
            StockChipConsensusSnapshot.instrument_id,
            StockChipConsensusSnapshot.trade_date,
            StockChipConsensusSnapshot.core_run_id,
            StockChipConsensusSnapshot.chip_payload,
            StockChipConsensusSnapshot.status,
            StockChipConsensusSnapshot.created_at,
        )
        .where(
            StockChipConsensusSnapshot.instrument_id == Instrument.id,
            StockChipConsensusSnapshot.status == "succeeded",
            StockChipConsensusSnapshot.algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION,
            # 五元组严格匹配（trade_date + core_run_id 必须与 latest_snap 一致）
            StockChipConsensusSnapshot.trade_date == snap_subq.c.trade_date,
            StockChipConsensusSnapshot.core_run_id == snap_subq.c.source_run_id,
        )
        .order_by(StockChipConsensusSnapshot.created_at.desc())
        .limit(1)
        .lateral("latest_chip")
    )


def _build_max_trade_date_subquery():
    """构建 PER-INSTRUMENT MAX(bar_daily.trade_date) 相关标量子查询，用于 fp_is_stale 计算。

    [P0-6 修复 2026-07-29] 不再使用 flat 中存储的 is_stale 占位值，
    改为 SQL 表达式：latest_snap.trade_date < MAX(bar_daily.trade_date)。

    [CHANGE-20260731-006] 修正为 PER-INSTRUMENT max（相关子查询）：
    - 旧实现使用全局 MAX(bar_daily.trade_date)，导致任一股票有更新日线时，
      所有股票的快照都被标记为 stale（即使该股票自身日线未更新）。
    - 正确语义：snapshot trade_date < 该股票自身的 MAX(bar_daily.trade_date)。
    - 与 _build_snap_lateral 同口径：通过 Instrument.id 相关引用实现 per-instrument。
    """
    return (
        select(func.max(BarDaily.trade_date))
        .where(BarDaily.instrument_id == Instrument.id)
        .scalar_subquery()
    )


def _build_fp_value_expr(
    fp_key: str,
    snap_subq: Any | None = None,
    chip_subq: Any | None = None,
    max_trade_date_subq: Any | None = None,
) -> ColumnElement:
    """构建 fp 字段取值表达式（基于 LATERAL JOIN 列引用）。

    [P0 收口 2026-07-29] 根据 source 类型从不同位置取值：
    - flat: snap_subq.c.summary_payload["first_pyramid_flat"][fp_key]（JSON 路径）
    - chip: chip_subq.c.chip_payload["chip_flat"][fp_key]（JSON 路径）
    - column: snap_subq.c.<column_name>（真实列，禁止 .astext）
    - literal: literal(value)（常量表达式）
    - computed: 动态 SQL 表达式（is_stale / chip_available）
    """
    spec = FP_QUERY_FIELD_SPECS[fp_key]
    source = spec["source"]

    if source == "flat":
        if snap_subq is None:
            raise ValueError(f"fp_key '{fp_key}' (source=flat) requires snap LATERAL JOIN")
        return snap_subq.c.summary_payload["first_pyramid_flat"][fp_key]
    elif source == "chip":
        if chip_subq is None:
            raise ValueError(f"fp_key '{fp_key}' (source=chip) requires chip LATERAL JOIN")
        return chip_subq.c.chip_payload["chip_flat"][fp_key]
    elif source == "column":
        if snap_subq is None:
            raise ValueError(f"fp_key '{fp_key}' (source=column) requires snap LATERAL JOIN")
        return getattr(snap_subq.c, spec["column"])
    elif source == "literal":
        return literal(spec["literal_value"])
    elif source == "computed":
        computed_kind = spec["computed_kind"]
        if computed_kind == "is_stale":
            # [P0-6] snap.trade_date < MAX(bar_daily.trade_date)
            if snap_subq is None or max_trade_date_subq is None:
                raise ValueError(
                    f"fp_key '{fp_key}' (computed=is_stale) requires snap LATERAL JOIN + max_trade_date_subquery"
                )
            return snap_subq.c.trade_date < max_trade_date_subq
        elif computed_kind == "chip_available":
            # [P0-4] chip 存在（严格五元组匹配）AND chip_payload.chip.available=true
            if chip_subq is None:
                # 无 chip JOIN 时固定返回 False（无匹配可能）
                return literal(False)
            return (chip_subq.c.id.isnot(None)) & (
                chip_subq.c.chip_payload["chip"]["available"].astext == "true"
            )
        else:
            raise ValueError(f"Unknown computed_kind '{computed_kind}' for fp_key '{fp_key}'")
    else:
        raise ValueError(f"Unknown source '{source}' for fp_key '{fp_key}'")


def _cast_fp_value(expr: ColumnElement, data_type: str, source: str) -> ColumnElement:
    """按 data_type 和 source 转换为可比较/可排序类型。

    [P0-1 修复 2026-07-29] 严格按 source 分别处理：
    - flat/chip: JSON 路径取值，先 .astext 再 cast 到目标类型
    - column: 真实列，已具备类型；text 类型（如 UUID）cast 为 Text；datetime/number 直接使用
    - literal: literal() 已带类型，直接使用
    - computed: 表达式已带类型（boolean），直接使用
    禁止对 column/literal/computed 调用 .astext（会破坏 PostgreSQL 类型推断）。
    """
    if source in ("flat", "chip"):
        # JSON 路径取值 → astext → cast 到目标类型
        if data_type in ("number", "percent"):
            return cast(expr.astext, Float)
        if data_type == "boolean":
            return cast(expr.astext, Boolean)
        # text / datetime / enum：astext 返回字符串
        return expr.astext
    if source == "column":
        # 真实列已具备类型
        if data_type == "text":
            # UUID 列需 cast 为 Text 以便字符串比较
            return cast(expr, Text)
        # datetime / number / boolean：直接使用列
        return expr
    if source == "literal":
        # literal(value) 已具备类型，直接使用
        return expr
    if source == "computed":
        # computed 表达式已具备类型（boolean 等），直接使用
        return expr
    return expr


def _build_fp_filter_conditions(
    fp_filters: list[FpFilterSpec],
    snap_subq: Any | None = None,
    chip_subq: Any | None = None,
    max_trade_date_subq: Any | None = None,
) -> list[ColumnElement[bool]]:
    """构建 fp 筛选 WHERE 条件列表（基于 LATERAL JOIN 列引用）。

    [CHANGE-20260730-013] 支持新操作符合同：
    - text: contains/not_contains/eq/neq/empty/not_empty
    - enum: eq/neq/in/not_in/empty/not_empty
    - boolean: eq/empty/not_empty
    - number/percent: eq/neq/gt/gte/lt/lte/between/empty/not_empty
    - datetime: date_eq/before/after/between/empty/not_empty
    - multi_enum: has_any/has_all/not_has_any/empty/not_empty（未实现 SQL，无字段使用）
    """
    conditions: list[ColumnElement[bool]] = []
    for f in fp_filters:
        spec = FP_QUERY_FIELD_SPECS[f.fp_key]
        data_type = spec["data_type"]
        source = spec["source"]
        raw_expr = _build_fp_value_expr(f.fp_key, snap_subq, chip_subq, max_trade_date_subq)
        typed_expr = _cast_fp_value(raw_expr, data_type, source)

        if f.operator == "empty":
            conditions.append(raw_expr.is_(None))
        elif f.operator == "not_empty":
            conditions.append(raw_expr.isnot(None))
        elif f.operator == "eq":
            if data_type in ("number", "percent"):
                conditions.append(typed_expr == float(f.value))  # type: ignore[arg-type]
            elif data_type == "boolean":
                conditions.append(typed_expr == (str(f.value).lower() in ("true", "1", "yes")))  # type: ignore[arg-type]
            else:
                conditions.append(typed_expr == f.value)  # type: ignore[arg-type]
        elif f.operator == "neq":
            # neq: 不等于值 OR 为空（语义：显示所有不匹配的，包括 NULL）
            if data_type in ("number", "percent"):
                conditions.append(or_(typed_expr != float(f.value), typed_expr.is_(None)))  # type: ignore[arg-type]
            elif data_type == "boolean":
                conditions.append(or_(typed_expr != (str(f.value).lower() in ("true", "1", "yes")), typed_expr.is_(None)))  # type: ignore[arg-type]
            else:
                conditions.append(or_(typed_expr != f.value, typed_expr.is_(None)))  # type: ignore[arg-type]
        elif f.operator in ("gt", "gte", "lt", "lte"):
            op_func = {
                "gt": lambda a, b: a > b,
                "gte": lambda a, b: a >= b,
                "lt": lambda a, b: a < b,
                "lte": lambda a, b: a <= b,
            }[f.operator]
            if data_type in ("number", "percent"):
                conditions.append(op_func(typed_expr, float(f.value)))  # type: ignore[arg-type]
            else:
                conditions.append(op_func(typed_expr, f.value))  # type: ignore[arg-type]
        elif f.operator == "between":
            if data_type in ("number", "percent"):
                conditions.append(typed_expr.between(float(f.value), float(f.value2)))  # type: ignore[arg-type]
            else:
                conditions.append(typed_expr.between(f.value, f.value2))  # type: ignore[arg-type]
        elif f.operator == "date_eq":
            # datetime 日期相等（字符串前 10 位匹配 YYYY-MM-DD）
            conditions.append(func.substr(typed_expr, 1, 10) == f.value)  # type: ignore[arg-type]
        elif f.operator == "before":
            conditions.append(typed_expr < f.value)  # type: ignore[arg-type]
        elif f.operator == "after":
            conditions.append(typed_expr > f.value)  # type: ignore[arg-type]
        elif f.operator == "in":
            values = [v.strip() for v in str(f.value).split(",") if v.strip()]
            conditions.append(typed_expr.in_(values))  # type: ignore[arg-type]
        elif f.operator == "not_in":
            # not_in: 不在列表中 OR 为空（语义：显示所有不匹配的，包括 NULL）
            values = [v.strip() for v in str(f.value).split(",") if v.strip()]
            conditions.append(or_(typed_expr.notin_(values), typed_expr.is_(None)))  # type: ignore[arg-type]
        elif f.operator == "contains":
            conditions.append(typed_expr.ilike(f"%{f.value}%"))  # type: ignore[arg-type]
        elif f.operator == "not_contains":
            conditions.append(~typed_expr.ilike(f"%{f.value}%"))  # type: ignore[arg-type]
    return conditions


def _build_fp_sort_expression(
    fp_sort_spec: FpSortSpec,
    snap_subq: Any | None = None,
    chip_subq: Any | None = None,
    max_trade_date_subq: Any | None = None,
) -> ColumnElement:
    """构建 fp 排序表达式（基于 LATERAL JOIN 列引用）。"""
    raw_expr = _build_fp_value_expr(fp_sort_spec.fp_key, snap_subq, chip_subq, max_trade_date_subq)
    spec = FP_QUERY_FIELD_SPECS[fp_sort_spec.fp_key]
    typed_expr = _cast_fp_value(raw_expr, spec["data_type"], spec["source"])
    return typed_expr


async def get_market_stocks(
    db: AsyncSession,
    user_id: UUID,
    scope: str,
    query: str | None,
    page: int,
    page_size: int,
    sort: str | None,
    state: str | None = None,
    industry: str | None = None,
    concept: str | None = None,
    fp_filter: str | None = None,
    fp_sort: str | None = None,
) -> MarketStocksResponse:
    """查询行情列表（服务端分页 + 批量加载，禁止 N+1）。

    [CHANGE-20260729-004 P0-1] 新增 fp_filter/fp_sort：
    - fp_filter: 第一金字塔字段服务端筛选，格式 key:op:val[;val2];key2:op2:val2
    - fp_sort: 第一金字塔字段服务端排序，格式 key:direction
    - 排序和筛选均通过 JSON 路径标量子查询，在分页前完成
    - asc/desc 均 NULLS LAST，第二排序键固定为 Instrument.symbol

    Args:
        db: 异步数据库会话
        user_id: 当前用户 ID（用于自选关联）
        scope: market | watchlist
        query: 搜索关键词（代码/名称/拼音首字母）
        page: 页码（从 1 开始）
        page_size: 每页大小
        sort: 排序字段:方向（如 symbol:asc）
        state: 状态筛选（Phase 4）：up/down/sideways
        industry: 行业筛选（板块名称，qstock 同步后可用）
        concept: 概念筛选（板块名称，qstock 同步后可用）
        fp_filter: 第一金字塔字段服务端筛选字符串
        fp_sort: 第一金字塔字段服务端排序字符串

    Returns:
        MarketStocksResponse 分页响应
    """
    search_conditions, rank_expr = _build_search_conditions(query)
    sort_spec = _parse_sort(sort)
    state_cond = _build_state_filter(state)
    board_conditions = _build_board_filter_conditions(industry, concept)
    # [CHANGE-20260729-004 P0-1] 解析 fp_filter/fp_sort（非法值抛 ValueError → 422）
    fp_filter_specs = _parse_fp_filter(fp_filter)
    fp_sort_spec = _parse_fp_sort(fp_sort)
    offset = (page - 1) * page_size

    # [CHANGE-20260729-005 二.7] 按需构建 LATERAL JOIN（最新 snapshot + chip 单次关联）
    # [P0 修复 2026-07-29] chip LATERAL 依赖 snap LATERAL（引用 snap.trade_date/source_run_id），
    # 因此 needs_chip=True 时必须也构建 snap LATERAL；max_trade_date_subq 用于 is_stale computed。
    needs_snap = _needs_snap_lateral(fp_filter_specs, fp_sort_spec) or _needs_chip_lateral(
        fp_filter_specs, fp_sort_spec,
    )
    needs_chip = _needs_chip_lateral(fp_filter_specs, fp_sort_spec)

    # [CHANGE-20260729-008] 优先读取已发布 stock_core pointer.data_run_id
    # 存在 pointer 时严格绑定该 run，禁止跨 run 混读；无 pointer 时回退每股 latest
    from app.services.factor_publication_service import (
        PUBLICATION_KIND_STOCK_CORE,
        SCOPE_TYPE_MARKET,
        get_publication,
    )

    published_core_run_id: UUID | None = None
    if needs_snap:
        pub = await get_publication(
            db,
            scope_type=SCOPE_TYPE_MARKET,
            scope_key="market",
            trade_date=None,  # 取最新 pointer，不限定 trade_date
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
        )
        if pub is not None:
            published_core_run_id = pub.data_run_id

    snap_subq = _build_snap_lateral(snapshot_run_id=published_core_run_id) if needs_snap else None
    chip_subq = _build_chip_lateral(snap_subq) if needs_chip else None
    max_trade_date_subq = _build_max_trade_date_subquery() if needs_snap else None
    # fp_filter_conditions 依赖 LATERAL JOIN 列引用，必须在 subq 创建后构建
    fp_filter_conditions = _build_fp_filter_conditions(
        fp_filter_specs, snap_subq, chip_subq, max_trade_date_subq,
    )

    # ===== Query 1: instruments + is_watchlisted + 分页 =====
    if scope == "watchlist":
        base_stmt = (
            select(
                Instrument.id,
                Instrument.symbol,
                Instrument.name,
                Instrument.market,
                literal(True).label("is_watchlisted"),
            )
            .join(
                UserWatchlistItem,
                (
                    (UserWatchlistItem.instrument_id == Instrument.id)
                    & (UserWatchlistItem.user_id == user_id)
                    & (UserWatchlistItem.active.is_(True))
                ),
            )
        )
    else:
        watched_exists = (
            select(1)
            .where(
                UserWatchlistItem.instrument_id == Instrument.id,
                UserWatchlistItem.user_id == user_id,
                UserWatchlistItem.active.is_(True),
            )
            .exists()
        )
        base_stmt = select(
            Instrument.id,
            Instrument.symbol,
            Instrument.name,
            Instrument.market,
            watched_exists.label("is_watchlisted"),
        )
    # [二.7] 添加 LATERAL JOIN（在 WHERE 之前，供 filter/sort 引用）
    if snap_subq is not None:
        base_stmt = base_stmt.outerjoin(snap_subq, true())
    if chip_subq is not None:
        base_stmt = base_stmt.outerjoin(chip_subq, true())

    for cond in search_conditions:
        base_stmt = base_stmt.where(cond)
    if state_cond is not None:
        base_stmt = base_stmt.where(state_cond)
    for cond in board_conditions:
        base_stmt = base_stmt.where(cond)
    for cond in fp_filter_conditions:
        base_stmt = base_stmt.where(cond)

    order_by_cols = _build_order_by(
        sort_spec, has_query=bool(query), rank_expr=rank_expr,
        fp_sort_spec=fp_sort_spec, snap_subq=snap_subq, chip_subq=chip_subq,
        max_trade_date_subq=max_trade_date_subq,
    )
    base_stmt = base_stmt.order_by(*order_by_cols)

    base_stmt = base_stmt.offset(offset).limit(page_size)
    base_result = await db.execute(base_stmt)
    base_rows = base_result.all()

    # 空页边界：page 超出总页数时 base_rows 为空，但仍需返回真实 total 和全局 as_of
    if not base_rows:
        count_stmt_empty = select(func.count(Instrument.id)).select_from(Instrument)
        if scope == "watchlist":
            count_stmt_empty = (
                select(func.count(Instrument.id))
                .select_from(Instrument)
                .join(
                    UserWatchlistItem,
                    (
                        (UserWatchlistItem.instrument_id == Instrument.id)
                        & (UserWatchlistItem.user_id == user_id)
                        & (UserWatchlistItem.active.is_(True))
                    ),
                )
            )
        if snap_subq is not None:
            count_stmt_empty = count_stmt_empty.outerjoin(snap_subq, true())
        if chip_subq is not None:
            count_stmt_empty = count_stmt_empty.outerjoin(chip_subq, true())
        for cond in search_conditions:
            count_stmt_empty = count_stmt_empty.where(cond)
        for cond in board_conditions:
            count_stmt_empty = count_stmt_empty.where(cond)
        if state_cond is not None:
            count_stmt_empty = count_stmt_empty.where(state_cond)
        for cond in fp_filter_conditions:
            count_stmt_empty = count_stmt_empty.where(cond)
        real_total = await db.scalar(count_stmt_empty) or 0

        empty_price_as_of = await db.scalar(select(func.max(BarDaily.trade_date)))
        empty_state_as_of = await db.scalar(
            select(func.max(StockFeatureSnapshot.created_at)).where(
                StockFeatureSnapshot.schema_version == _SCHEMA_VERSION
            )
        )
        empty_boards_as_of = await db.scalar(
            select(func.max(MarketBoard.updatedAt))
        )

        return MarketStocksResponse(
            items=[],
            page=page,
            page_size=page_size,
            total=real_total,
            price_as_of=empty_price_as_of.isoformat() if empty_price_as_of else None,
            state_as_of=to_shanghai_iso(empty_state_as_of) if empty_state_as_of else None,
            boards_as_of=to_shanghai_iso(empty_boards_as_of) if empty_boards_as_of else None,
        )

    instrument_ids = [row.id for row in base_rows]
    id_to_row = {row.id: row for row in base_rows}

    # ===== Query 2: count =====
    count_stmt = select(func.count(Instrument.id)).select_from(Instrument)
    if scope == "watchlist":
        count_stmt = (
            select(func.count(Instrument.id))
            .select_from(Instrument)
            .join(
                UserWatchlistItem,
                (
                    (UserWatchlistItem.instrument_id == Instrument.id)
                    & (UserWatchlistItem.user_id == user_id)
                    & (UserWatchlistItem.active.is_(True))
                ),
            )
        )
    if snap_subq is not None:
        count_stmt = count_stmt.outerjoin(snap_subq, true())
    if chip_subq is not None:
        count_stmt = count_stmt.outerjoin(chip_subq, true())
    for cond in search_conditions:
        count_stmt = count_stmt.where(cond)
    if state_cond is not None:
        count_stmt = count_stmt.where(state_cond)
    for cond in board_conditions:
        count_stmt = count_stmt.where(cond)
    for cond in fp_filter_conditions:
        count_stmt = count_stmt.where(cond)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # ===== Query 3: 最新 2 根日线（批量） =====
    bars_subq = (
        select(
            BarDaily.instrument_id,
            BarDaily.trade_date,
            BarDaily.close,
            func.row_number()
            .over(
                partition_by=BarDaily.instrument_id,
                order_by=BarDaily.trade_date.desc(),
            )
            .label("rn"),
        )
        .where(BarDaily.instrument_id.in_(instrument_ids))
        .subquery()
    )
    bars_stmt = select(bars_subq).where(bars_subq.c.rn <= 2)
    bars_result = await db.execute(bars_stmt)

    price_map: dict[UUID, tuple[float | None, float | None]] = {}
    for bar_row in bars_result:
        inst_id = bar_row.instrument_id
        close_val = float(bar_row.close) if bar_row.close is not None else None
        existing = price_map.get(inst_id)
        if existing is None:
            # rn=1 (latest)
            price_map[inst_id] = (close_val, None)
        else:
            # rn=2 (previous)
            latest, _ = existing
            price_map[inst_id] = (latest, close_val)

    # ===== Query 4: 最新 feature snapshot（批量，含 first_pyramid） =====
    # [PRD §三 列表视图第一金字塔全量字段] summary_payload.first_pyramid 已在盘后写入，
    # 此处批量读取后扁平化为 99 个 fp_ 键，禁止 N+1 逐股请求。
    snap_subq = (
        select(
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.summary_payload,
            StockFeatureSnapshot.created_at,
            StockFeatureSnapshot.trade_date,
            StockFeatureSnapshot.source_run_id,
            func.row_number()
            .over(
                partition_by=StockFeatureSnapshot.instrument_id,
                order_by=StockFeatureSnapshot.trade_date.desc(),
            )
            .label("rn"),
        )
        .where(
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            # [CHANGE-20260718-007] 使用 _SCHEMA_VERSION，禁止硬编码
            StockFeatureSnapshot.schema_version == _SCHEMA_VERSION,
        )
        .subquery()
    )
    snap_stmt = select(snap_subq).where(snap_subq.c.rn == 1)
    snap_result = await db.execute(snap_stmt)

    state_map: dict[UUID, tuple[str | None, str | None, dict[str, Any] | None, date | None, UUID | None]] = {}
    # 全局最新 trade_date 用于判断快照过期（来自 Query 7 的预查询）
    # 此处先用 None 占位，实际过期判定在组装响应阶段对比 price_as_of_date
    for snap_row in snap_result:
        payload = snap_row.summary_payload or {}
        dsa_state = _map_dsa_state(payload.get("daily_developing_swing_dir"))
        structure_state = payload.get("cost_position_zone")
        # 第一金字塔扁平化：summary_payload.first_pyramid 是嵌套 dict
        first_pyramid_raw = payload.get("first_pyramid")
        flat_fp: dict[str, Any] | None = None
        if first_pyramid_raw is not None:
            # is_stale 由调用方在组装响应时通过 trade_date 对比 price_as_of 判定；
            # 此处先用 False 占位，组装阶段修正
            flat_fp = flatten_first_pyramid(
                first_pyramid_raw,
                calculated_at=to_shanghai_iso(snap_row.created_at)
                if snap_row.created_at
                else None,
                run_id=str(snap_row.source_run_id) if snap_row.source_run_id else None,
                is_stale=False,
            )
            # [P0-2 修复 2026-07-29 二.2] fp_trade_date 改用 snapshot.trade_date 真实列
            # （不读 first_pyramid.tradeDate，确保 DB 筛选/排序与响应同口径）
            if snap_row.trade_date is not None:
                flat_fp["fp_trade_date"] = snap_row.trade_date.isoformat()
        state_map[snap_row.instrument_id] = (
            dsa_state,
            str(structure_state) if structure_state else None,
            flat_fp,
            snap_row.trade_date,
            snap_row.source_run_id,
        )

    # ===== Query 4b: 严格五元组匹配的 chip 记录批量查询（禁止 N+1） =====
    # [P0-2 修复 2026-07-29 二.5/二.6] chip 必须按以下五元组严格匹配 latest_snap：
    #   instrument_id, trade_date=latest_snap.trade_date,
    #   core_run_id=latest_snap.source_run_id,
    #   algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION
    # [CHANGE-20260729-009] 移除 status=="succeeded" 过滤，改为查询任意状态 chip：
    #   - succeeded: 合并 chip_flat 到 first_pyramid（原逻辑）
    #   - skipped/failed: 构建 chip_status 结构化状态（M15_BARS_INSUFFICIENT 等）
    # 唯一约束 (instrument_id, trade_date, core_run_id, algorithm_version) 保证每股最多 1 条。
    chip_map: dict[UUID, Any | None] = dict.fromkeys(instrument_ids)
    # 仅对有 snap 且 source_run_id 非空的 instrument 查询 chip
    snap_for_chip = [
        (iid, snap_data[3], snap_data[4])
        for iid, snap_data in state_map.items()
        if snap_data[3] is not None and snap_data[4] is not None
    ]
    if snap_for_chip:
        chip_stmt = (
            select(
                StockChipConsensusSnapshot.instrument_id,
                StockChipConsensusSnapshot.chip_payload,
                StockChipConsensusSnapshot.status,
                StockChipConsensusSnapshot.error_message,
                StockChipConsensusSnapshot.created_at,
            )
            .where(
                StockChipConsensusSnapshot.algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION,
                # 四元组严格匹配（algorithm_version 已在 WHERE 中）：
                # (instrument_id, trade_date, core_run_id)
                tuple_(
                    StockChipConsensusSnapshot.instrument_id,
                    StockChipConsensusSnapshot.trade_date,
                    StockChipConsensusSnapshot.core_run_id,
                ).in_([
                    (iid, td, rid) for iid, td, rid in snap_for_chip
                ]),
            )
        )
        chip_result = await db.execute(chip_stmt)
        for chip_row in chip_result:
            chip_map[chip_row.instrument_id] = chip_row

    # ===== Query 4c: DSA 策略结果 payload（批量，用于 DSA 列展示） =====
    # [CHANGE-20260729-009] /market/stocks 作为列表唯一数据源，
    # 需返回原 DSA 字段（dsa_dir_bars/vwap_ret_avg 等）。
    # 从最新已发布 dsa_selector run 的 strategy_results 表批量读取 payload。
    dsa_payload_map: dict[UUID, dict[str, Any]] = {}
    try:
        latest_dsa_run_id: UUID | None = await db.scalar(
            select(StrategyRun.id)
            .where(
                StrategyRun.strategy_key == _DSA_STRATEGY_KEY,
                StrategyRun.status == "published",
            )
            .order_by(StrategyRun.trade_date.desc())
            .limit(1)
        )
        if latest_dsa_run_id is not None and instrument_ids:
            dsa_stmt = (
                select(
                    StrategyResult.instrument_id,
                    StrategyResult.payload,
                )
                .where(
                    StrategyResult.run_id == latest_dsa_run_id,
                    StrategyResult.instrument_id.in_(instrument_ids),
                )
            )
            dsa_result = await db.execute(dsa_stmt)
            for dsa_row in dsa_result:
                dsa_payload_map[dsa_row.instrument_id] = dsa_row.payload or {}
    except Exception:
        logger.warning("[MarketStocks] 查询 DSA payload 失败，payload 字段将为 None", exc_info=True)

    # ===== Query 4d: 日线计数（仅 flat_fp=None 的股票，用于区分 INSUFFICIENT_DAILY_BARS vs COMPUTE_FAILED） =====
    # [CHANGE-20260729-009] 109 只新股 first_pyramid=null，需返回 actual_bars/required_bars 结构化原因
    daily_bar_count_map: dict[UUID, int] = {}
    instruments_needing_bar_count = [
        iid for iid in instrument_ids
        if state_map.get(iid, (None, None, None, None, None))[2] is None
    ]
    if instruments_needing_bar_count:
        bar_count_stmt = (
            select(
                BarDaily.instrument_id,
                func.count().label("cnt"),
            )
            .where(BarDaily.instrument_id.in_(instruments_needing_bar_count))
            .group_by(BarDaily.instrument_id)
        )
        bar_count_result = await db.execute(bar_count_stmt)
        for row in bar_count_result:
            daily_bar_count_map[row.instrument_id] = row.cnt

    # ===== Query 5: 板块归属（批量，industry/concepts） =====
    boards_map = await get_instrument_boards_batch(db, instrument_ids)

    # ===== Query 6/7/8: 全局 as_of（提前到组装前，供 first_pyramid.is_stale 判定） =====
    boards_as_of_dt: datetime | None = await db.scalar(
        select(func.max(MarketBoard.updatedAt))
    )
    # price_as_of_date 是全局 MAX(bar_daily.trade_date)，用于响应 price_as_of 字段
    # （"行情数据最新到哪天"——全市场口径，非单股）
    price_as_of_date: date | None = await db.scalar(select(func.max(BarDaily.trade_date)))
    state_as_of_dt: datetime | None = await db.scalar(
        select(func.max(StockFeatureSnapshot.created_at)).where(
            # [CHANGE-20260718-007] 使用 _SCHEMA_VERSION，禁止硬编码
            StockFeatureSnapshot.schema_version == _SCHEMA_VERSION
        )
    )

    # [CHANGE-20260731-006] PER-INSTRUMENT MAX(bar_daily.trade_date) 用于 is_stale 判定。
    # 旧实现用全局 price_as_of_date 判 is_stale，导致任一股票有更新日线时所有快照都 stale。
    # 正确语义：每只股票的快照只与自身的最新日线比较。与 DB 筛选口径一致（_build_max_trade_date_subquery 已改相关子查询）。
    # 仅当存在快照时才查询（无快照则 is_stale 不计算，避免无谓 SQL）。
    inst_max_bar_date_map: dict[UUID, date] = {}
    has_snapshots = any(entry[2] is not None for entry in state_map.values())
    if has_snapshots and instrument_ids:
        inst_max_bar_stmt = (
            select(
                BarDaily.instrument_id,
                func.max(BarDaily.trade_date).label("max_date"),
            )
            .where(BarDaily.instrument_id.in_(instrument_ids))
            .group_by(BarDaily.instrument_id)
        )
        inst_max_bar_result = await db.execute(inst_max_bar_stmt)
        # [CHANGE-20260731-006] 使用独立循环变量名 bar_row 避免与上方 row (Row[tuple[UUID,int]])
        # 的类型冲突；func.max(Date) 在 SQLAlchemy 类型系统中被推断为 int，需 cast 到 date。
        for bar_row in inst_max_bar_result:
            inst_max_bar_date_map[bar_row.instrument_id] = _typing.cast("date", bar_row.max_date)

    # ===== 组装响应 =====
    items: list[MarketStockRow] = []
    for inst_id in instrument_ids:
        base = id_to_row[inst_id]
        latest_price, prev_close = price_map.get(inst_id, (None, None))

        change_pct: float | None = None
        if latest_price is not None and prev_close is not None and prev_close != 0:
            change_pct = round((latest_price - prev_close) / prev_close * 100, 2)

        dsa_state, structure_state, flat_fp, snap_td, snap_run_id = state_map.get(
            inst_id, (None, None, None, None, None)
        )

        # [CHANGE-20260731-006] PER-INSTRUMENT is_stale：快照 trade_date 早于该股票自身最新日线 trade_date
        # 旧实现用全局 price_as_of_date 判定，导致任一股票有更新日线时所有快照都 stale。
        # 与 DB 筛选同口径（_build_max_trade_date_subquery 已改相关子查询）。
        if flat_fp is not None:
            inst_max_bar = inst_max_bar_date_map.get(inst_id)
            if snap_td is not None and inst_max_bar is not None:
                flat_fp["fp_is_stale"] = snap_td < inst_max_bar
            else:
                flat_fp["fp_is_stale"] = False

        # [P0-3 修复 2026-07-29 二.6] 合并 matched chip 的 chip_flat 到 first_pyramid
        # 保证用于 filter/sort 的数据与返回 first_pyramid 中的 10 个 chip 字段完全一致
        # [P0-4 修复 2026-07-29 二.4] fp_chip_available 改为 computed 表达式：
        # 只在存在严格匹配（五元组）且 chip_payload.chip.available=true 的 succeeded 记录时为 True
        # [CHANGE-20260729-009] chip_map 现存储完整 chip row（含 status/error_message/created_at），
        # 仅 succeeded 状态才合并 chip_flat；任意状态都构建 chip_status 结构化状态。
        # [CHANGE-20260730-010] chip_status 与 /first-pyramid 详情 API 完全同口径：
        # - chip_row 存在：调用共享 _build_chip_status_from_row → camelCase ChipStatus
        # - chip_row 为 None 但 flat_fp 存在（有 snap 但 chip job 未跑）：state=pending
        # - chip_row 为 None 且 flat_fp 为 None（无 snap）：chip_status=None
        chip_row = chip_map.get(inst_id)
        chip_status_struct: dict[str, Any] | None = _build_chip_status_struct(chip_row)
        if chip_status_struct is None and flat_fp is not None:
            # 有快照但无 chip 记录：chip job 尚未执行（与详情 API resolve_chip_status 一致）
            from app.schemas.first_pyramid import ChipStatus as _ChipStatusSchema
            chip_status_struct = _ChipStatusSchema(
                state="pending",
                reasonCode="CHIP_JOB_PENDING",
                reasonText="筹码任务尚未执行",
                computedAt=None,
            ).model_dump(by_alias=False)
        if flat_fp is not None:
            if chip_row is not None and chip_row.status == "succeeded":
                chip_payload = chip_row.chip_payload
                chip_flat = chip_payload.get("chip_flat") or {} if isinstance(chip_payload, dict) else {}
                chip_dim = chip_payload.get("chip") if isinstance(chip_payload, dict) else None
                chip_available = bool(
                    chip_dim is not None
                    and isinstance(chip_dim, dict)
                    and chip_dim.get("available") is True
                )
                # 用 matched chip 的 chip_flat 覆盖 10 个 chip 字段
                for k in FP_CHIP_KEYS:
                    if k in chip_flat:
                        flat_fp[k] = chip_flat[k]
                flat_fp["fp_chip_available"] = chip_available
            else:
                # 无匹配 chip 或非 succeeded：所有 chip 字段保持 None，fp_chip_available=False
                for k in FP_CHIP_KEYS:
                    flat_fp[k] = None
                flat_fp["fp_chip_available"] = False

        # [CHANGE-20260729-009] 计算 factor_ready/factor_error + 填充 data_run_id/payload/chip_status
        # flat_fp=None 时查询日线计数以区分 INSUFFICIENT_DAILY_BARS vs COMPUTE_FAILED
        daily_count = daily_bar_count_map.get(inst_id) if flat_fp is None else None
        factor_ready, factor_error, factor_actual_bars, factor_required_bars = (
            _compute_factor_ready(flat_fp, daily_count)
        )
        dsa_payload = dsa_payload_map.get(inst_id)

        # 板块归属：industry 取首个行业，concepts 取全部概念
        inst_boards = boards_map.get(inst_id, [])
        industry_name = next(
            (b["name"] for b in inst_boards if b["type"] == "industry"), None
        )
        concept_names = [b["name"] for b in inst_boards if b["type"] == "concept"]

        items.append(
            MarketStockRow(
                instrument_id=inst_id,
                symbol=base.symbol,
                name=base.name,
                latest_price=latest_price,
                change_pct=change_pct,
                industry=industry_name,
                concepts=concept_names,
                dsa_state=dsa_state,
                structure_state=structure_state,
                latest_event_title=None,
                latest_event_time=None,
                is_watchlisted=base.is_watchlisted,
                first_pyramid=flat_fp,
                payload=dsa_payload,
                data_run_id=snap_run_id,
                factor_ready=factor_ready,
                factor_error=factor_error,
                factor_actual_bars=factor_actual_bars,
                factor_required_bars=factor_required_bars,
                chip_status=chip_status_struct,
            )
        )

    return MarketStocksResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        price_as_of=price_as_of_date.isoformat() if price_as_of_date else None,
        state_as_of=to_shanghai_iso(state_as_of_dt) if state_as_of_dt else None,
        boards_as_of=to_shanghai_iso(boards_as_of_dt) if boards_as_of_dt else None,
    )


# ===== C9: 板块目录只读 API =====


async def _get_board_sync_status_from_job(
    db: AsyncSession,
) -> dict[str, object] | None:
    """从最近 after-close job metadata 回退读取板块同步状态。

    PR #77 收口 §三.4：Redis 缺失/重启时，从 SchedulerJobRun.metadata_json
    中的 board_sync_result 字段回退得到 source 和 last_attempt_status。

    Args:
        db: 异步 DB 会话

    Returns:
        board_sync_result dict 或 None（无任何 after-close job 含板块同步结果）
    """
    try:
        stmt = (
            select(SchedulerJobRun)
            .where(SchedulerJobRun.job_name == "after_close_orchestrator")
            .where(SchedulerJobRun.metadata_json.isnot(None))
            .order_by(SchedulerJobRun.created_at.desc())
            .limit(20)
        )
        result = await db.execute(stmt)
        job_runs = result.scalars().all()
        for job_run in job_runs:
            if not job_run.metadata_json:
                continue
            try:
                meta = json.loads(job_run.metadata_json)
            except (json.JSONDecodeError, TypeError):
                continue
            board_result = meta.get("board_sync_result")
            if isinstance(board_result, dict):
                return board_result
    except Exception:
        logger.warning("[MarketStocks] 从 job metadata 回退板块同步状态失败", exc_info=True)
    return None


async def get_market_boards(
    db: AsyncSession,
    board_type: str | None = None,
) -> MarketBoardsResponse:
    """读取板块目录（只读），供前端行业/概念筛选下拉使用。

    从 market_boards 表查询全部行业/概念板块，按 name 升序。
    扩展响应（PROMPT §五.4）：source/stale/last_attempt_status。

    stale 语义：旧数据存在而最新同步失败时 available=true, stale=true，仍允许筛选。
    从未成功才 available=false。

    Args:
        db: 异步 DB 会话
        board_type: 可选类型过滤（industry | concept），None 返回全部
    """
    from app.services.board_sync_service import get_sync_status

    stmt = select(MarketBoard).order_by(MarketBoard.name.asc())
    if board_type in ("industry", "concept"):
        stmt = stmt.where(MarketBoard.type == board_type)

    result = await db.execute(stmt)
    boards = result.scalars().all()

    updated_at_dt: datetime | None = await db.scalar(
        select(func.max(MarketBoard.updatedAt))
    )

    # 读取 Redis 中的最近同步状态
    sync_status = await get_sync_status()
    last_attempt_status = sync_status.get("status") if sync_status else None
    source = sync_status.get("source") if sync_status else None

    # PR #77 收口 §三.4：Redis 缺失/重启时从最近 after-close job metadata 回退
    # 不让 Redis 成为唯一事实源——DB 已有数据时不得 source=None
    if sync_status is None:
        fallback = await _get_board_sync_status_from_job(db)
        if fallback is not None:
            last_attempt_status = fallback.get("status")
            source = fallback.get("source")

    # stale: 旧数据存在 + 最新同步失败/降级
    has_data = len(boards) > 0
    is_stale = has_data and last_attempt_status in ("failed", "degraded")

    # DB 有数据但无任何状态来源（Redis 和 job metadata 均无）时，source 标记为 unknown
    final_source = source if has_data else None
    if has_data and final_source is None:
        final_source = "unknown"

    return MarketBoardsResponse(
        items=[
            MarketBoardItem(
                id=b.id,
                name=b.name,
                type=b.type,
                external_code=b.externalCode,
            )
            for b in boards
        ],
        available=has_data,
        reason_code=None if has_data else "board_provider_unavailable",
        updated_at=to_shanghai_iso(updated_at_dt) if updated_at_dt else None,
        source=final_source,
        stale=is_stale,
        last_attempt_status=last_attempt_status if has_data else None,
    )
