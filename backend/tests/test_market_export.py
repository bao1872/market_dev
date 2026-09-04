"""行情 Excel 导出端点契约测试（CHANGE-20260904 MARKET EXCEL EXPORT FIX）。

验证 /v1/market/export 复用 /market/stocks 同一查询 owner（get_market_stocks），
fp 筛选/排序走 fp_filter/fp_sort 而非 DSA 旧路径 metric_filters：
1. fp_volume_zscore20 筛选导出不触发旧 metric_filters 白名单 422；
2. fp 筛选经 fp_filter 语义透传（服务收到 fp_filter，非 metric_filters）；
3. fp 排序经 fp_sort 语义透传；
4. fp 可见列值来自 canonical first_pyramid（导出行含该值，非 None）；
5. 无筛选导出正常；
6. 行数超过 MAX_EXPORT_ROWS 返回 422。

本测试 PURE（PURE_UNIT_TEST=1）：monkeypatch get_market_stocks + generate_xlsx + override 认证与 get_db，
使用 httpx.AsyncClient + ASGITransport 绕过 lifespan 与真实数据库，不触达 bz_stock。
（xlsx 为 ZIP 压缩流，不在原始字节中 grep 数值；故改 monkeypatch generate_xlsx 捕获 data_rows 验证 fp 值来源。）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.deps import get_db
from app.main import app
from app.schemas.market_stocks import MarketStockRow, MarketStocksResponse

pytestmark = pytest.mark.pure_unit

_USER_ID = "00000000-0000-0000-0000-00000000000a"


def _fake_row(fp_value: float = 0.81) -> MarketStockRow:
    return MarketStockRow(
        instrument_id=UUID("00000000-0000-0000-0000-0000000000b1"),
        symbol="600519",
        name="贵州茅台",
        latest_price=1800.0,
        change_pct=2.3,
        industry="白酒",
        concepts=["消费"],
        dsa_state="上行",
        structure_state="cost",
        is_watchlisted=False,
        first_pyramid={"fp_volume_zscore20": fp_value, "fp_trend_strength": 0.5},
        payload=None,
        data_run_id=None,
        factor_ready=True,
        factor_error=None,
        factor_actual_bars=100,
        factor_required_bars=60,
        chip_status=None,
    )


def _fake_response(items: list[MarketStockRow]) -> MarketStocksResponse:
    return MarketStocksResponse(
        items=items,
        page=1,
        page_size=10001,
        total=len(items),
        price_as_of=None,
        state_as_of=None,
        boards_as_of=None,
    )


def _export_body(fp_filter: str | None = None, fp_sort: str | None = None) -> dict:
    columns = [
        {"key": "stock", "title": "股票", "data_type": "text", "payload_key": None},
        {"key": "fp_volume_zscore20", "title": "量能Z", "data_type": "number", "payload_key": None},
    ]
    return {
        "scope": "market",
        "keyword": None,
        "industry": None,
        "concept": None,
        "state": None,
        "fp_filter": fp_filter,
        "fp_sort": fp_sort,
        "sort": None,
        "stock_name": None,
        "stock_name_op": None,
        "visible_columns": columns,
    }


@pytest_asyncio.fixture
async def export_client():
    """PURE 客户端：httpx.AsyncClient + ASGITransport 绕过 lifespan；override 认证与 get_db。"""
    from app.services.access_control_service import AccessContext, require_authenticated

    def _fake_auth() -> AccessContext:
        return AccessContext(
            user_id=_USER_ID,
            account_status="active",
            roles=["member"],
            is_admin=False,
            is_member=True,
            subscription_active=True,
            plan_code="observe_20",
            plan_display_name="观察版",
            expires_at=None,
            features=[],
            limits={},
            capabilities={
                "self_selection": {"active": True},
                "market_data": {"active": True},
            },
            default_route="/market",
            active_capability_keys=["self_selection", "market_data"],
            capability_source="user_capabilities",
            diagnostics=[],
        )

    async def _fake_db():
        yield None

    app.dependency_overrides[require_authenticated] = _fake_auth
    app.dependency_overrides[get_db] = _fake_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(require_authenticated, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_fp_volume_zscore20_filter_export_no_metric_filter_422(export_client: AsyncClient) -> None:
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response([_fake_row()])),
    ) as mock_svc, patch(
        "app.services.excel_export_service.generate_xlsx",
        new=MagicMock(return_value=b"xlsx"),
    ) as mock_xlsx:
        resp = await export_client.post(
            "/v1/market/export", json=_export_body(fp_filter="fp_volume_zscore20:gt:0.5")
        )
    assert resp.status_code == 200, resp.text
    # fp 筛选经 fp_filter 透传，非 DSA 旧路径 metric_filters
    assert mock_svc.call_args.kwargs["fp_filter"] == "fp_volume_zscore20:gt:0.5"
    # fp 可见列值来自 canonical first_pyramid（0.81），非 None
    data_rows = mock_xlsx.call_args.args[1]
    assert data_rows[0]["fp_volume_zscore20"] == 0.81


@pytest.mark.asyncio
async def test_fp_sort_routed_through_fp_sort(export_client: AsyncClient) -> None:
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response([_fake_row()])),
    ) as mock_svc:
        resp = await export_client.post(
            "/v1/market/export", json=_export_body(fp_sort="fp_trend_strength:desc")
        )
    assert resp.status_code == 200, resp.text
    assert mock_svc.call_args.kwargs["fp_sort"] == "fp_trend_strength:desc"


@pytest.mark.asyncio
async def test_no_filter_export_works(export_client: AsyncClient) -> None:
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response([_fake_row()])),
    ), patch(
        "app.services.excel_export_service.generate_xlsx",
        new=MagicMock(return_value=b"xlsx"),
    ) as mock_xlsx:
        resp = await export_client.post("/v1/market/export", json=_export_body())
    assert resp.status_code == 200, resp.text
    # 无筛选时 fp 值仍来自 canonical first_pyramid（证明导出阅源与列表页同源）
    data_rows = mock_xlsx.call_args.args[1]
    assert data_rows[0]["fp_volume_zscore20"] == 0.81


@pytest.mark.asyncio
async def test_export_returns_full_filtered_result_not_just_page(export_client: AsyncClient) -> None:
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response([_fake_row()])),
    ) as mock_svc:
        resp = await export_client.post("/v1/market/export", json=_export_body())
    assert resp.status_code == 200
    # 导出请求全量结果（page=1, page_size=MAX_EXPORT_ROWS+1），非单页
    assert mock_svc.call_args.kwargs["page_size"] == 10001
    assert mock_svc.call_args.kwargs["page"] == 1


@pytest.mark.asyncio
async def test_export_row_limit_enforced(export_client: AsyncClient) -> None:
    many = [_fake_row(fp_value=float(i)) for i in range(10001)]
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response(many)),
    ):
        resp = await export_client.post("/v1/market/export", json=_export_body())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_export_visible_fp_column_from_canonical_first_pyramid(export_client: AsyncClient) -> None:
    with patch(
        "app.api.market.get_market_stocks",
        new=AsyncMock(return_value=_fake_response([_fake_row()])),
    ), patch(
        "app.services.excel_export_service.generate_xlsx",
        new=MagicMock(return_value=b"xlsx"),
    ) as mock_xlsx:
        body = _export_body()
        body["visible_columns"] = [
            {"key": "stock", "title": "股票", "data_type": "text", "payload_key": None},
            {"key": "fp_trend_strength", "title": "趋势强度", "data_type": "number", "payload_key": None},
        ]
        resp = await export_client.post("/v1/market/export", json=body)
    assert resp.status_code == 200, resp.text
    # fp_trend_strength 可见列值来自 canonical first_pyramid（0.5，非 None）
    data_rows = mock_xlsx.call_args.args[1]
    assert data_rows[0]["fp_trend_strength"] == 0.5
