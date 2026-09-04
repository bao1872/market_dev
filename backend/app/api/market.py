"""市场状态 API（交易时段、交易日判断）。

提供：
- GET /market/status: 获取当前市场状态（交易日、交易时段、状态文本、日历诊断信息）

设计说明：
- 交易日判断：使用 is_trading_day_async 三级降级（DB -> Mootdx -> weekday）
- 交易时段判断：复用 app.services.market_status_service.compute_market_session（6 值枚举）
- 状态文本映射：NON_TRADING_DAY->休市、PRE_OPEN->盘前、MORNING_SESSION->交易中、
  LUNCH_BREAK->午间休市、AFTERNOON_SESSION->交易中、MARKET_CLOSED->已收盘、
  UNKNOWN->交易日历待确认
- 日历诊断：当 DB 中 status=UNKNOWN 时返回 degraded=True，不显示"休市"
- market_session：6 值枚举（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）
- 时区：统一使用 app.core.time 的上海时区工具，避免散落的 ZoneInfo 实例
"""

from __future__ import annotations

from datetime import date as dt_date

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.route_utils import get_route_paths
from app.core.time import now_shanghai, shanghai_business_date, to_shanghai_iso
from app.models.calendar import TradingCalendar
from app.services.calendar_service import is_trading_day_async
from app.services.market_status_service import compute_market_session

router = APIRouter(prefix="/v1/market", tags=["market"])


class MarketStatusResponse(BaseModel):
    """市场状态响应"""
    is_trading_day: bool
    is_trading_hours: bool
    status_text: str  # "交易中" / "已收盘" / "休市" / "盘前" / "交易日历待确认"
    market_session: str  # 6 值枚举（与 watchlist monitor-status 对齐）
    # [Calendar] - 描述: 交易日历诊断字段
    calendar_date: dt_date = Field(..., description="当前日历日期")
    calendar_status: str | None = Field(None, description="DB 中日历状态 OPEN/CLOSED/UNKNOWN")
    calendar_source: str | None = Field(None, description="DB 中日历来源")
    calendar_verified_at: str | None = Field(None, description="DB 中日历最近确认时间 ISO")
    degraded: bool = Field(False, description="是否处于降级状态")
    degraded_reason: str | None = Field(None, description="降级原因")


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status(db: AsyncSession = Depends(get_db)):
    """获取当前市场状态

    交易日判断：使用 trading_calendar 表 + Mootdx + weekday 三级降级
    交易时段判断：复用 compute_market_session（6 值枚举，与 watchlist 对齐）
    """
    today = shanghai_business_date()
    now = now_shanghai()

    # 交易日判断（bool，可能经过降级）
    is_trading_day = await is_trading_day_async(db, today)

    # 查询 DB 原始日历记录用于诊断展示
    degraded = False
    degraded_reason: str | None = None
    calendar_status: str | None = None
    calendar_source: str | None = None
    calendar_verified_at: str | None = None

    try:
        stmt = select(
            TradingCalendar.status,
            TradingCalendar.source,
            TradingCalendar.verified_at,
        ).where(
            TradingCalendar.trade_date == today,
            TradingCalendar.market == "A",
        )
        result = await db.execute(stmt)
        row = result.first()
        if row:
            calendar_status, calendar_source, verified_at = row
            if verified_at is not None:
                calendar_verified_at = to_shanghai_iso(verified_at)
            if calendar_status == "UNKNOWN":
                degraded = True
                degraded_reason = "calendar status UNKNOWN"
                # [市场状态] - 描述: UNKNOWN 时不显示休市，返回待确认文案
                market_session = compute_market_session(now, is_trading_day=True)
            else:
                market_session = compute_market_session(now, is_trading_day)
        else:
            # DB 无记录，is_trading_day 已降级到 Mootdx/weekday
            degraded = True
            degraded_reason = "calendar not in DB"
            market_session = compute_market_session(now, is_trading_day)
    except Exception as exc:
        # [市场状态] - 描述: DB 诊断查询失败不影响主体返回，记录降级原因
        degraded = True
        degraded_reason = f"calendar diagnostics unavailable: {exc}"
        market_session = compute_market_session(now, is_trading_day)

    # is_trading_hours：仅上午/下午盘为 True（用于向后兼容）
    is_trading_hours = market_session in ("MORNING_SESSION", "AFTERNOON_SESSION")

    # 状态文本统一映射
    status_text_map = {
        "NON_TRADING_DAY": "休市",
        "PRE_OPEN": "盘前",
        "MORNING_SESSION": "交易中",
        "LUNCH_BREAK": "午间休市",
        "AFTERNOON_SESSION": "交易中",
        "MARKET_CLOSED": "已收盘",
    }
    if degraded and calendar_status == "UNKNOWN":
        status_text = "交易日历待确认"
    else:
        status_text = status_text_map.get(market_session, "未知")

    return MarketStatusResponse(
        is_trading_day=is_trading_day,
        is_trading_hours=is_trading_hours,
        status_text=status_text,
        market_session=market_session,
        calendar_date=today,
        calendar_status=calendar_status,
        calendar_source=calendar_source,
        calendar_verified_at=calendar_verified_at,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={get_route_paths(router.routes)}")
    print("OK")


# ===== 行情列表 API（PRD §8.1）=====

from uuid import UUID  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.schemas.market_stocks import (  # noqa: E402
    MarketBoardsResponse,
    MarketExportRequest,
    MarketStocksResponse,
)
from app.services.access_control_service import (  # noqa: E402
    AccessContext,
    require_any_capability,
    require_authenticated,
)
from app.services.market_stocks_service import get_market_stocks  # noqa: E402

# Phase 4: state 筛选合法值（up=上行, down=下行, sideways=震荡）
_VALID_STATE_FILTERS = {"up", "down", "sideways"}


@router.get("/stocks", response_model=MarketStocksResponse)
async def list_market_stocks(
    scope: str = Query("market", description="范围：market | watchlist"),
    query: str | None = Query(None, description="搜索关键词（代码/名称/拼音首字母）"),
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(50, ge=1, le=100, description="每页大小（最大 100）"),
    sort: str | None = Query(
        None,
        description="排序字段:方向（如 symbol:asc, change_pct:desc, dsa_state:desc, latest_event_time:desc）",
    ),
    industry: str | None = Query(None, description="行业筛选（板块名称，qstock 同步后可用）"),
    concept: str | None = Query(None, description="概念筛选（板块名称，qstock 同步后可用）"),
    state: str | None = Query(
        None,
        description="状态筛选（Phase 4 实现）：up=上行, down=下行, sideways=震荡",
    ),
    fp_filter: str | None = Query(
        None,
        description=(
            "[CHANGE-20260729-004] 第一金字塔字段服务端筛选，"
            "格式 key:op:val[;val2];key2:op2:val2。"
            "支持字段见 FP_QUERY_FIELD_SPECS；非法字段/操作符返回 422"
        ),
    ),
    fp_sort: str | None = Query(
        None,
        description=(
            "[CHANGE-20260729-004] 第一金字塔字段服务端排序，"
            "格式 key:direction。支持字段见 FP_QUERY_FIELD_SPECS"
        ),
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_any_capability("self_selection", "market_data")),
) -> MarketStocksResponse:
    """查询行情列表（服务端分页 + 批量加载，禁止 N+1）。

    返回每行页面所需全部字段（价格/涨跌幅/DSA状态/事件/自选），不再追加单股请求。
    scope=watchlist 在数据库查询阶段关联当前用户自选（INNER JOIN）。
    state 参数已实现（Phase 4）：up/down/sideways。
    industry/concept 参数已实现（PRD §7.5 qstock 同步后）：通过 market_boards 表筛选。
    [CHANGE-20260729-004] fp_filter/fp_sort：通过 JSON 路径标量子查询在分页前完成第一金字塔字段筛选/排序；
    所有排序均 NULLS LAST，第二排序键固定为 Instrument.symbol 保证翻页稳定。
    sort 白名单：name, symbol, change_pct, dsa_state, latest_event_time。
    """
    # Phase 4: state 参数校验（合法值：up/down/sideways；空字符串视为 None）
    normalized_state = state or None
    if normalized_state is not None and normalized_state not in _VALID_STATE_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid state value: {state}; must be one of: up, down, sideways",
        )

    # 规范化 scope
    normalized_scope = "watchlist" if scope == "watchlist" else "market"
    try:
        return await get_market_stocks(
            db=db,
            user_id=UUID(ctx.user_id),
            scope=normalized_scope,
            query=query,
            page=page,
            page_size=page_size,
            sort=sort,
            state=normalized_state,
            industry=industry or None,
            concept=concept or None,
            fp_filter=fp_filter or None,
            fp_sort=fp_sort or None,
        )
    except ValueError as exc:
        # [CHANGE-20260730-013] 筛选/排序参数校验失败 → 422 结构化错误
        from app.services.first_pyramid_flatten import FpFilterValidationError

        if isinstance(exc, FpFilterValidationError):
            detail = exc.to_detail()
        else:
            detail = {"message": str(exc)}
        raise HTTPException(status_code=422, detail=detail) from exc


# ===== [CHANGE-20260904] 行情 Excel 导出（复用 /market/stocks 同一查询语义与 canonical 行源）=====
# 旧导出走 /strategy-runs/{run_id}/results/export，把 fp_* 筛选转成 metric_filters 后
# 经 StrategyVersion.manifest.outputs.filterable 白名单校验 → fp_* 不在白名单 → 422。
# 新端点直接复用 get_market_stocks（/market/stocks 的查询 owner）：fp_filter/fp_sort 由服务内部
# 按 FP_QUERY_FIELD_SPECS 校验（与列表页同源），fp_* 可见列从 MarketStockRow.first_pyramid 读取。
# 不新增第二套 fp 解析，不写 fp_* 到 DSA strategy manifest。


@router.post("/export")
async def export_market_stocks(
    request: MarketExportRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_any_capability("self_selection", "market_data")),
) -> Response:
    """导出行情筛选结果为 .xlsx（复用 /market/stocks 同一查询语义）。

    与 GET /market/stocks 共享 get_market_stocks 查询 owner：
    - fp_filter / fp_sort：第一金字塔字段筛选/排序（FP_QUERY_FIELD_SPECS 白名单）
    - scope / keyword / industry / concept / state / stock_name：与列表页一致
    - sort：基础排序字段:方向
    导出全量筛选结果（非当前页），上限 MAX_EXPORT_ROWS。
    fp_* 可见列从 canonical first_pyramid 读取；基础列从行字段读取。

    Args:
        request: 导出请求（含筛选/排序/可见列）
        db: 异步会话
        ctx: 权限上下文

    Returns:
        .xlsx 文件流
    """
    from urllib.parse import quote

    from app.services.excel_export_service import (
        MAX_EXPORT_ROWS,
        extract_market_row_data,
        generate_xlsx,
    )
    from app.services.first_pyramid_flatten import FpFilterValidationError

    normalized_scope = "watchlist" if request.scope == "watchlist" else "market"
    try:
        result = await get_market_stocks(
            db=db,
            user_id=UUID(ctx.user_id),
            scope=normalized_scope,
            query=request.keyword,
            page=1,
            page_size=MAX_EXPORT_ROWS + 1,
            sort=request.sort,
            state=request.state,
            industry=request.industry,
            concept=request.concept,
            fp_filter=request.fp_filter,
            fp_sort=request.fp_sort,
        )
    except ValueError as exc:
        # fp_filter/fp_sort 校验失败（FP_QUERY_FIELD_SPECS）→ 422，与列表页同源
        if isinstance(exc, FpFilterValidationError):
            detail = exc.to_detail()
        else:
            detail = {"message": str(exc)}
        raise HTTPException(status_code=422, detail=detail) from exc

    items = result.items
    # 上限校验（与旧 export 一致）：超过 MAX_EXPORT_ROWS 拒绝
    if len(items) > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"导出行数 {len(items)} 超过上限 {MAX_EXPORT_ROWS}，"
                "请缩小筛选范围后再导出"
            ),
        )

    data_rows = [
        extract_market_row_data(row, request.visible_columns) for row in items
    ]
    xlsx_bytes = generate_xlsx(request.visible_columns, data_rows)

    filename = "盘迹_行情_筛选结果.xlsx"
    quoted_filename = quote(filename, safe="")
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}",
            "Content-Length": str(len(xlsx_bytes)),
            "X-Source-Total": str(result.total),
            "X-Filtered-Total": str(result.total),
            "X-Export-Rows": str(len(data_rows)),
        },
    )


# ===== [CHANGE-20260730-013] 字段元数据 API =====


@router.get("/filter-specs")
async def get_market_filter_specs(
    ctx: AccessContext = Depends(require_authenticated),
) -> dict:
    """返回第一金字塔 99 字段的筛选元数据（含 operators/enum_values/input_control）。

    普通用户即可读取（不需要 admin）；用于前端动态生成类型化筛选器控件。
    """
    from app.services.first_pyramid_flatten import serialize_fp_query_field_specs

    return serialize_fp_query_field_specs()


# ===== C9: 板块目录只读 API（行业/概念筛选下拉支持）=====


@router.get("/boards", response_model=MarketBoardsResponse)
async def list_market_boards(
    type: str | None = Query(
        None,
        description="板块类型过滤：industry | concept（不传返回全部）",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> MarketBoardsResponse:
    """板块目录只读 API（C9），供前端行业/概念筛选下拉/自动完成使用。

    从 market_boards 表读取板块目录，qstock 同步前返回空列表（不报错）。
    """
    if type is not None and type not in ("industry", "concept"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid type value: {type}; must be one of: industry, concept",
        )
    from app.services.market_stocks_service import get_market_boards

    return await get_market_boards(db=db, board_type=type)
