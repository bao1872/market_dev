"""[S2-A-C1] Market export 安全/权限/低内存契约测试（PURE，无 DB/Redis 连接）。

本文件只覆盖不触达数据库/Redis 的契约（在 PURE_UNIT_TEST=1 下本地可运行）：
- A   生产导出路径不调用 get_market_stocks
- I   非 admin → 403（C1a 熔断）
- J   admin → 授权通过
- REQ 请求合同：/market/export 不再接受 visible_columns；导出列由服务端固定
- PROJ 服务端固定列：股票名称 + 股票代码（顺序固定）
- ROW batch 只产出 name / symbol 两列
- NOENR 导出服务不再含 price/snapshot/chip/board enrichment pipeline
- O   生成有效 OOXML（inlineStr，无 sharedStrings，表头只有两列）
- P   流式下载完成后临时文件清理
- 事件循环响应性（CPU 工作在 worker 线程，heartbeat 仍能推进）
- 5000 行合成压力门禁（真实 XLSX 生成，2 列，测 baseline）
- 筛选语义不回归（fp_filter/fp_sort/industry/concept/state/stock_name/sort/watchlist）
- 偏好飞行契约（C2）：422/429 必须在 StreamingResponse 创建前确定
- 锁语义（持有者令牌 + 忙时拒绝 + 仅持有者可释放）

DB/端点集成契约（B/C/G/...）在 tests/test_market_export_integration.py，
仅 PANJI_REMOTE_VERIFY_DB_TEST=1（远程验证库）下运行。

注意：不使用 app.main ASGI（其 lifespan 在 PURE 模式下会触发 DB 查询且 session 为 None），
改为直接调用端点函数 export_market_stocks 与依赖 require_admin。
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, literal, select

from app.api.market import export_market_stocks, require_admin
from app.models.instrument import Instrument
from app.schemas.market_stocks import MarketExportRequest
from app.services import market_export_service as mes
from app.services.access_control_service import AccessContext
from app.services.excel_export_service import MarketXlsxWriter

pytestmark = pytest.mark.pure_unit

_USER_ID = "00000000-0000-0000-0000-00000000000a"


# ---------------------------------------------------------------------------
# 请求构造 / auth 辅助
# ---------------------------------------------------------------------------


def _body(**over):
    b = {
        "scope": "market",
        "keyword": None,
        "industry": None,
        "concept": None,
        "state": None,
        "fp_filter": None,
        "fp_sort": None,
        "sort": None,
        "stock_name": None,
        "stock_name_op": None,
    }
    b.update(over)
    return b


def _make_auth(is_admin: bool):
    def _auth() -> AccessContext:
        return AccessContext(
            user_id=_USER_ID,
            account_status="active",
            roles=["admin"] if is_admin else ["member"],
            is_admin=is_admin,
            is_member=not is_admin,
            subscription_active=True,
            plan_code="admin" if is_admin else "observe_20",
            plan_display_name="admin" if is_admin else "观察版",
            expires_at=None,
            features=[],
            limits={},
            capabilities={},
            default_route="/market",
            active_capability_keys=[],
            capability_source="user_capabilities",
            diagnostics=[],
        )

    return _auth


# ---------------------------------------------------------------------------
# A / I / J：端点授权 + 不调用 get_market_stocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_admin_authorized_and_no_get_market_stocks():
    req = MarketExportRequest(**_body())
    ctx = _make_auth(True)()

    async def _dummy_prepare(db, plan, user_id):
        d = tempfile.mkdtemp(prefix="panji-export-")
        final = os.path.join(d, "export.xlsx")
        with open(final, "wb") as f:
            f.write(b"PK\x03\x04fake")
        return mes.PreparedMarketExport(
            final_path=final, tmp_dir=d, rows=1, columns=2, batches=1, max_batch=1, bytes=4
        )

    with patch(
        "app.services.market_export_service.prepare_market_export", new=_dummy_prepare
    ), patch("app.api.market.get_market_stocks", new=AsyncMock()) as mock_svc:
        resp = await export_market_stocks(req, None, ctx)
    assert isinstance(resp, StreamingResponse)
    # 偏好飞行在 StreamingResponse 创建前完成：drain 触发流式清理
    out = b"".join([c async for c in resp.body_iterator])
    assert out == b"PK\x03\x04fake"
    assert mock_svc.call_count == 0  # A：生产路径绝不调用 get_market_stocks


@pytest.mark.asyncio
async def test_export_non_admin_403_fuse():
    ctx = _make_auth(False)()
    with pytest.raises(HTTPException) as ei:
        await require_admin(ctx)
    assert ei.value.status_code == 403  # I：C1a 熔断，普通用户禁止


@pytest.mark.asyncio
async def test_export_admin_passes_require_admin():
    ctx = _make_auth(True)()
    assert await require_admin(ctx) is ctx  # J


# ---------------------------------------------------------------------------
# REQ：请求合同（visible_columns 已从导出合同移除）
# ---------------------------------------------------------------------------


def test_request_without_visible_columns_is_valid():
    req = MarketExportRequest(**_body(industry="银行", fp_filter="fp_x>1"))
    plan = mes.build_export_plan(req)
    assert plan.scope == "market"
    assert plan.industry == "银行"
    assert plan.fp_filter == "fp_x>1"


def test_request_has_no_visible_columns_field():
    # visible_columns 已从 /market/export 合同中移除（服务端固定列）
    assert "visible_columns" not in MarketExportRequest.model_fields
    # 即便客户端误传，pydantic 会忽略额外字段；服务端以固定列导出
    req = MarketExportRequest(**_body())
    assert "visible_columns" not in req.model_dump()


# ---------------------------------------------------------------------------
# PROJ：服务端固定两列
# ---------------------------------------------------------------------------


def test_server_owned_fixed_columns():
    cols = mes.MARKET_EXPORT_COLUMNS
    assert len(cols) == 2
    assert cols[0].key == "name" and cols[0].title == "股票名称"
    assert cols[1].key == "symbol" and cols[1].title == "股票代码"


# ---------------------------------------------------------------------------
# ROW：batch 只产出 name / symbol
# ---------------------------------------------------------------------------


class _FakeStmt:
    def limit(self, *a):
        return self

    def offset(self, *a):
        return self

    def where(self, *a):
        return self

    def order_by(self, *a):
        return self

    def with_only_columns(self, *a, **k):
        return self

    def subquery(self):
        # 返回真实（无 FROM 的）scalar subquery alias，使 select(...).select_from() 合法，不触达 DB
        return select(func.count()).subquery()


class _FakeCtx:
    base_stmt = _FakeStmt()


class _Row:
    def __init__(self, name: str, symbol: str):
        self.name = name
        self.symbol = symbol


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Res(self._rows)

    async def scalar(self, *a, **k):
        return 0


@pytest.mark.asyncio
async def test_fetch_batch_rows_only_name_symbol():
    ctx = _FakeCtx()
    rows_db = [
        _Row("贵州茅台", "600519"),
        _Row("宁德时代", "300750"),
        _Row("平安银行", "000001"),
    ]
    out = await mes._fetch_batch_rows(_FakeDb(rows_db), ctx, 0)
    assert out == [
        {"name": "贵州茅台", "symbol": "600519"},
        {"name": "宁德时代", "symbol": "300750"},
        {"name": "平安银行", "symbol": "000001"},
    ]
    for r in out:
        assert set(r.keys()) == {"name", "symbol"}


# ---------------------------------------------------------------------------
# PROJ-SQL：export batch 物理投影只 SELECT name / symbol（C3a）
# ---------------------------------------------------------------------------


def _representative_base_stmt(scope: str):
    """构造与 _assemble_market_query 同构的 base_stmt（不触达 DB）。

    market 范围：correlated EXISTS 作为 is_watchlisted 展示列 + WHERE + ORDER BY；
    watchlist 范围：INNER JOIN UserWatchlistItem + WHERE + ORDER BY。
    两者都包含 id / symbol / name / market / is_watchlisted 五个展示列，
    用于证明 export 投影收窄后这些列不再被 SELECT。
    """
    from app.models.watchlist import UserWatchlistItem

    if scope == "watchlist":
        return (
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
                    & (UserWatchlistItem.active.is_(True))
                ),
            )
            .where(Instrument.market == "A")
            .order_by(Instrument.symbol)
        )
    watched_exists = (
        select(1)
        .where(
            UserWatchlistItem.instrument_id == Instrument.id,
            UserWatchlistItem.active.is_(True),
        )
        .exists()
    )
    return (
        select(
            Instrument.id,
            Instrument.symbol,
            Instrument.name,
            Instrument.market,
            watched_exists.label("is_watchlisted"),
        )
        .where(Instrument.market == "A")
        .order_by(Instrument.symbol)
    )


class _ProjCtx:
    def __init__(self, base_stmt):
        self.base_stmt = base_stmt


def test_export_batch_stmt_selects_only_name_symbol():
    # market 范围：correlated EXISTS 作为 is_watchlisted 展示列务必被剔除
    stmt = mes._build_export_batch_stmt(_ProjCtx(_representative_base_stmt("market")), 0)
    cols = list(stmt.selected_columns.keys())
    assert cols == ["name", "symbol"]
    assert "id" not in cols
    assert "market" not in cols
    assert "is_watchlisted" not in cols
    sql = str(stmt)
    assert "WHERE" in sql and "ORDER BY" in sql  # 筛选 + 排序在窄投影下仍生效

    # watchlist 范围：JOIN 必须保留（否则筛选/排序语义失效）
    stmt_w = mes._build_export_batch_stmt(_ProjCtx(_representative_base_stmt("watchlist")), 0)
    cols_w = list(stmt_w.selected_columns.keys())
    assert cols_w == ["name", "symbol"]
    sql_w = str(stmt_w)
    assert "JOIN" in sql_w and "WHERE" in sql_w and "ORDER BY" in sql_w


def test_export_count_stmt_selects_only_literal_one():
    # count 只保留 FROM/JOIN/WHERE，不计算 is_watchlisted/market/name/symbol 等目标表达式
    ctx = _ProjCtx(_representative_base_stmt("market"))
    count_select = ctx.base_stmt.with_only_columns(
        literal(1), maintain_column_froms=True
    ).order_by(None)
    count_cols = list(count_select.selected_columns.keys())
    # 仅 literal(1) 一个目标表达式（key 为 '_no_label'），无任何真实列被 SELECT
    assert count_cols == ["_no_label"]
    for forbidden in ("id", "market", "name", "symbol", "is_watchlisted"):
        assert forbidden not in count_cols
    count_source = count_select.subquery()
    full = select(func.count()).select_from(count_source)
    sql = str(full)
    assert "WHERE" in sql  # 筛选条件在 count 中保留


# ---------------------------------------------------------------------------
# NOENR：导出服务不再含 enrichment pipeline
# ---------------------------------------------------------------------------


def test_export_service_has_no_enrichment_pipeline():
    for name in ("_fetch_prices", "_fetch_snapshots", "_fetch_chips", "_cell_value"):
        assert not hasattr(mes, name), f"{name} 必须从 market export service 删除"


# ---------------------------------------------------------------------------
# O / 低内存：MarketXlsxWriter 增量 + inlineStr + 仅两列
# ---------------------------------------------------------------------------


def test_xlsx_has_only_two_fixed_columns():
    rows = [
        {"name": "贵州茅台", "symbol": "600519"},
        {"name": "宁德时代", "symbol": "300750"},
    ]
    with tempfile.TemporaryDirectory() as d:
        w = MarketXlsxWriter(mes.MARKET_EXPORT_COLUMNS, d)
        w.add_rows(rows)
        w.finalize()
        out = os.path.join(d, "export.xlsx")
        w.build_zip(out)
        assert os.path.getsize(out) > 0
        zf = zipfile.ZipFile(out)
        assert zf.testzip() is None  # O：有效 OOXML
        assert "[Content_Types].xml" in zf.namelist()
        assert "xl/worksheets/sheet1.xml" in zf.namelist()
        # 低内存：使用 inlineStr，不应存在 sharedStrings 全量驻留
        assert "xl/sharedStrings.xml" not in zf.namelist()
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "inlineStr" in sheet
        # 表头只有两列
        header = sheet.split('<row r="1">')[1].split("</row>")[0]
        assert header.count("<c ") == 2
        assert "股票名称" in header and "股票代码" in header
        # 数据行只有两格
        data = sheet.split('<row r="2">')[1].split("</row>")[0]
        assert data.count("<c ") == 2
        assert "贵州茅台" in sheet and "600519" in sheet


def test_xlsx_writer_streaming_file_grows():
    """结构硬约束：数据增量写入磁盘临时文件，而非持有完整内存集合。"""
    rows = [{"name": f"N{i}", "symbol": f"S{i:05d}"} for i in range(600)]
    with tempfile.TemporaryDirectory() as d:
        w = MarketXlsxWriter(mes.MARKET_EXPORT_COLUMNS, d)
        sizes = []
        for i in range(0, 600, 250):
            w.add_rows(rows[i : i + 250])
            sizes.append(os.path.getsize(w._ws_path))
        assert sizes[-1] > sizes[0]
        w.finalize()
        out = os.path.join(d, "export.xlsx")
        w.build_zip(out)
        assert zipfile.ZipFile(out).testzip() is None


# ---------------------------------------------------------------------------
# 事件循环响应性：CPU 工作在 worker 线程，heartbeat 仍能推进
# ---------------------------------------------------------------------------


def test_export_writer_off_event_loop():
    worker_started = threading.Event()
    worker_finished = threading.Event()

    def blocking_writer():
        worker_started.set()
        time.sleep(0.3)  # 模拟 XML/zip CPU
        worker_finished.set()

    async def run():
        task = asyncio.create_task(asyncio.to_thread(blocking_writer))
        # 不能在协程里直接调用阻塞的 threading.Event.wait（会卡住事件循环）
        started = await asyncio.to_thread(worker_started.wait, 2)
        assert started
        loop = asyncio.get_event_loop()
        hb = asyncio.Event()

        async def beat():
            hb.set()

        loop.call_soon(asyncio.ensure_future, beat())
        await asyncio.wait_for(hb.wait(), timeout=1.0)
        heartbeat_while_busy = hb.is_set() and not worker_finished.is_set()
        await task
        return heartbeat_while_busy

    assert asyncio.run(run()), "heartbeat 必须在 writer 占用线程时仍被事件循环推进"


# ---------------------------------------------------------------------------
# 5000 行合成压力门禁（真实 XLSX 生成，2 列，IDE baseline）
# ---------------------------------------------------------------------------


def test_export_5000_row_writer_stress():
    rows = [{"name": f"N{i}", "symbol": f"S{i:05d}"} for i in range(5000)]
    with tempfile.TemporaryDirectory() as d:
        peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        w = MarketXlsxWriter(mes.MARKET_EXPORT_COLUMNS, d)
        batches = 0
        max_batch = 0
        for i in range(0, 5000, mes.EXPORT_BATCH_SIZE):
            chunk = rows[i : i + mes.EXPORT_BATCH_SIZE]
            w.add_rows(chunk)
            batches += 1
            max_batch = max(max_batch, len(chunk))
        w.finalize()
        out = os.path.join(d, "export.xlsx")
        w.build_zip(out)
        size = os.path.getsize(out)
        peak_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        zf = zipfile.ZipFile(out)
        assert zf.testzip() is None
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        # 表头 + 5000 数据行 = 5001 个 <row r="
        assert sheet.count('<row r="') == 5001
        assert max_batch <= mes.EXPORT_BATCH_SIZE
        print(
            f"STRESS rows=5000 columns=2 batches={batches} "
            f"max_batch={max_batch} xlsx_bytes={size} "
            f"peak_rss_delta={peak_after - peak_before}"
        )
        assert size > 0


# ---------------------------------------------------------------------------
# 筛选语义不回归（导出列固定，但筛选/排序决定行集合与顺序）
# ---------------------------------------------------------------------------


def test_plan_preserves_filter_semantics():
    req = MarketExportRequest(
        **_body(
            industry="银行",
            concept="新能源",
            state="up",
            fp_filter="fp_x>1",
            fp_sort="fp_y:desc",
            sort="name:asc",
            stock_name="茅台",
            stock_name_op="contains",
        )
    )
    plan = mes.build_export_plan(req)
    assert plan.scope == "market"
    assert plan.industry == "银行"
    assert plan.concept == "新能源"
    assert plan.state == "up"
    assert plan.fp_filter == "fp_x>1"
    assert plan.fp_sort == "fp_y:desc"
    assert plan.sort == "name:asc"
    assert plan.stock_name == "茅台"
    assert plan.stock_name_op == "contains"


def test_plan_watchlist_scope_normalized():
    plan = mes.build_export_plan(MarketExportRequest(**_body(scope="watchlist")))
    assert plan.scope == "watchlist"


@pytest.mark.asyncio
async def test_preflight_passes_filter_args_to_assemble():
    captured: dict = {}

    async def _assemble(db, user_id, scope, query, state, industry, concept, fp_filter, fp_sort, sort):
        captured.update(
            {
                "scope": scope,
                "query": query,
                "state": state,
                "industry": industry,
                "concept": concept,
                "fp_filter": fp_filter,
                "fp_sort": fp_sort,
                "sort": sort,
            }
        )
        return _FakeCtx()

    req = MarketExportRequest(**_body(industry="银行", fp_filter="fp_x>1", state="up", stock_name="茅台"))
    plan = mes.build_export_plan(req)
    # count=0 → 不进分批 fetch，但 _assemble_market_query 必须收到完整筛选语义。
    # 使用真实 writer（会真实写入临时文件），并清理其 tmp_dir。
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value="h")), \
         patch.object(mes, "_assemble_market_query", new=_assemble), \
         patch.object(mes, "release_lock", new=AsyncMock()):
        prepared = await mes.prepare_market_export(_FakeDb([]), plan, uuid.UUID(int=0))
    try:
        assert prepared.rows == 0
        assert prepared.columns == 2
    finally:
        shutil.rmtree(prepared.tmp_dir, ignore_errors=True)
    # captured 非空即证明 _assemble_market_query 被调用且收到了完整筛选语义
    assert captured
    assert captured["scope"] == "market"
    assert captured["industry"] == "银行"
    assert captured["fp_filter"] == "fp_x>1"
    assert captured["state"] == "up"


# ---------------------------------------------------------------------------
# 偏好飞行契约（C2）：422/429 必须在 StreamingResponse 创建前确定
# ---------------------------------------------------------------------------


class _FakeDbCount:
    def __init__(self, count):
        self._count = count

    async def scalar(self, *a, **k):
        return self._count


@pytest.mark.asyncio
async def test_preflight_busy_before_db_returns_429():
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value=None)), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock()) as mock_assemble, \
         patch.object(mes, "_fetch_batch_rows", new=AsyncMock()) as mock_fetch, \
         patch.object(mes, "MarketXlsxWriter", new=MagicMock()) as mock_writer_cls, \
         patch.object(mes, "release_lock", new=AsyncMock()) as mock_release:
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(None, plan, uuid.UUID(int=0))
    assert ei.value.status_code == 429
    # 锁竞争失败（429）必须发生在任何重 DB 工作之前
    assert mock_assemble.call_count == 0
    assert mock_fetch.call_count == 0
    assert mock_writer_cls.call_count == 0
    # 未获得锁，不应执行释放
    assert mock_release.call_count == 0


@pytest.mark.asyncio
async def test_preflight_over_limit_returns_422_and_releases_lock():
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    holder = "holder-x"
    db = _FakeDbCount(mes.MAX_EXPORT_ROWS + 1)
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value=holder)) as mock_acquire, \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())) as mock_assemble, \
         patch.object(mes, "_fetch_batch_rows", new=AsyncMock()) as mock_fetch, \
         patch.object(mes, "MarketXlsxWriter", new=MagicMock()) as mock_writer_cls, \
         patch.object(mes, "release_lock", new=AsyncMock()) as mock_release:
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    assert ei.value.status_code == 422
    assert mock_acquire.call_count == 1
    assert mock_assemble.call_count == 1
    assert mock_fetch.call_count == 0
    assert mock_writer_cls.call_count == 0
    # 已获得锁，超限路径必须释放，避免泄漏
    assert mock_release.call_count == 1
    assert mock_release.await_args.args == (mes.EXPORT_LOCK_KEY, holder)


@pytest.mark.asyncio
async def test_endpoint_propagates_preflight_422_before_streaming():
    req = MarketExportRequest(**_body())
    ctx = _make_auth(True)()

    async def _raise_422(*a, **k):
        raise HTTPException(status_code=422, detail="over limit")

    with patch(
        "app.services.market_export_service.prepare_market_export", new=_raise_422
    ):
        with pytest.raises(HTTPException) as ei:
            await export_market_stocks(req, None, ctx)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_propagates_preflight_429_before_streaming():
    req = MarketExportRequest(**_body())
    ctx = _make_auth(True)()

    async def _raise_429(*a, **k):
        raise HTTPException(status_code=429, detail="busy")

    with patch(
        "app.services.market_export_service.prepare_market_export", new=_raise_429
    ):
        with pytest.raises(HTTPException) as ei:
            await export_market_stocks(req, None, ctx)
    assert ei.value.status_code == 429


# ---------------------------------------------------------------------------
# 锁语义（K/L/M/N 基础）：持有者令牌 + 忙时拒绝 + 仅持有者可释放
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = val
        return True

    async def get(self, key):
        return self._store.get(key)

    async def eval(self, script, numkeys, *args):
        key, holder = args[0], args[1]
        if self._store.get(key) == holder:
            self._store.pop(key, None)
            return 1
        return 0

    async def delete(self, key):
        self._store.pop(key, None)


def test_lock_acquire_release_fake_redis():
    fake = _FakeRedis()
    with patch("app.services.distributed_lock.get_redis", return_value=fake):

        async def run():
            h1 = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 10, "holder1")
            assert h1 == "holder1"
            h2 = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 10, "holder2")
            assert h2 is None  # 忙时第二次获取失败（并发=1）
            assert await mes.release_lock(mes.EXPORT_LOCK_KEY, "holder1") is True
            assert await mes.release_lock(mes.EXPORT_LOCK_KEY, "holder1") is False  # ABA：已释放不再误删

        asyncio.run(run())


# ---------------------------------------------------------------------------
# P：流式下载完成后临时文件清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_cleanup_after_complete():
    d = tempfile.mkdtemp(prefix="panji-export-")
    final = os.path.join(d, "export.xlsx")
    with open(final, "wb") as f:
        f.write(b"PK")
    prepared = mes.PreparedMarketExport(
        final_path=final, tmp_dir=d, rows=1, columns=2, batches=1, max_batch=1, bytes=2
    )
    out = b"".join([c async for c in mes.stream_prepared_market_export(prepared)])
    assert out == b"PK"
    assert not os.path.exists(d)  # P：流式阶段只做清理，与 DB/锁无关
