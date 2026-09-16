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
改为直接调用端点函数 export_market_stocks 与授权依赖 require_market_export_access。
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
from sqlalchemy import literal, select

from app.api.market import export_market_stocks, require_market_export_access
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


def _cap_ctx(*capabilities: str) -> AccessContext:
    """构造带显式 capability 的普通用户 ctx（capability 判权矩阵用）。"""
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
            cap: {"active": True, "expires_at": None, "watchlist_limit": None}
            for cap in capabilities
        },
        default_route="/market",
        active_capability_keys=list(capabilities),
        capability_source="user_capabilities",
        diagnostics=[],
    )


# ---------------------------------------------------------------------------
# A / I / J / K：端点授权（C1b capability 判权）+ 不调用 get_market_stocks
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
async def test_export_non_admin_without_capability_403():
    """I（C1b 恢复）：无任何 capability 的普通用户 → 403。

    C1a 的 admin-only 临时熔断已解除，改由 capability 判权；无权限仍然拒绝
    （恢复授权 ≠ 放开给所有人）。
    """
    ctx = _make_auth(False)()  # capabilities={}
    for scope in ("market", "watchlist"):
        with pytest.raises(HTTPException) as ei:
            await require_market_export_access(
                request=MarketExportRequest(**_body(scope=scope)), ctx=ctx
            )
        assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_export_admin_passes_any_scope():
    """J（Case 1）：admin 豁免，无需任何 capability，market / watchlist 均放行。"""
    ctx = _make_auth(True)()
    for scope in ("market", "watchlist"):
        assert (
            await require_market_export_access(
                request=MarketExportRequest(**_body(scope=scope)), ctx=ctx
            )
            is ctx
        )


@pytest.mark.asyncio
async def test_export_capability_scope_matrix():
    """Case 2/3：capability 与 scope 严格对应（复用 _authorize_market_scope 唯一 SSOT）。

    - market_data → scope=market 放行；scope=watchlist → 403（无 self_selection）
    - self_selection → scope=watchlist 放行；scope=market → 403（严禁隐式获得全市场）
    """
    market_ctx = _cap_ctx("market_data")
    self_ctx = _cap_ctx("self_selection")

    assert (
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="market")), ctx=market_ctx
        )
        is market_ctx
    )
    with pytest.raises(HTTPException) as ei:
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="watchlist")), ctx=market_ctx
        )
    assert ei.value.status_code == 403

    assert (
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="watchlist")), ctx=self_ctx
        )
        is self_ctx
    )
    with pytest.raises(HTTPException) as ei:
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="market")), ctx=self_ctx
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_export_authorization_uses_body_scope_ssot():
    """P0-4 SSOT：授权的唯一输入是 request body.scope（与查询执行同源）。

    同一个 ctx 仅改 body.scope 即可让结论翻转（market_data：market 放行 / watchlist 403），
    证明授权确实由 body.scope 决定，不会被请求中任何其它 scope 来源改写。
    """
    ctx = _cap_ctx("market_data")
    assert (
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="market")), ctx=ctx
        )
        is ctx
    )
    with pytest.raises(HTTPException) as ei:
        await require_market_export_access(
            request=MarketExportRequest(**_body(scope="watchlist")), ctx=ctx
        )
    assert ei.value.status_code == 403


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

    def where(self, *a):
        return self

    def with_only_columns(self, *a, **k):
        return self


class _FakeCtx:
    def __init__(self):
        self.base_stmt = _FakeStmt()


class _Row:
    def __init__(self, name: str, symbol: str):
        self.name = name
        self.symbol = symbol


class _FakeStreamResult:
    """Fake AsyncResult: supports .partitions(size) async-gen + .close() coroutine."""

    def __init__(self, rows, partition_size=None):
        self._rows = rows
        self._partition_size = partition_size or mes.EXPORT_BATCH_SIZE
        self.closed = False
        self.partition_count = 0

    def partitions(self, size=None):
        size = size or self._partition_size

        async def _gen():
            for i in range(0, len(self._rows), size):
                self.partition_count += 1
                yield self._rows[i : i + size]

        return _gen()

    async def close(self):
        self.closed = True


class _FakeDb:
    """Records stream calls; C4 removed the count/scalar path entirely."""

    def __init__(self, rows=None, result=None, stream_calls=None):
        self._rows = list(rows or [])
        self._result = result
        self.stream_calls = stream_calls if stream_calls is not None else []
        self.scalar_calls = []
        self.last_result = None

    async def stream(self, stmt):
        self.stream_calls.append(stmt)
        result = self._result if self._result is not None else _FakeStreamResult(self._rows)
        self.last_result = result
        return result


@pytest.mark.asyncio
async def test_export_rows_stmt_selects_only_name_symbol_and_limit_10001():
    # 单条流式导出 statement：只 SELECT name/symbol，LIMIT 10001，无 OFFSET
    for scope in ("market", "watchlist"):
        ctx = _ProjCtx(_representative_base_stmt(scope))
        stmt = mes._build_export_rows_stmt(ctx)
        cols = list(stmt.selected_columns.keys())
        assert cols == ["name", "symbol"]
        assert "id" not in cols and "market" not in cols and "is_watchlisted" not in cols
        assert stmt._limit == mes.MAX_EXPORT_ROWS + 1
        sql = str(stmt)
        assert "OFFSET" not in sql
        assert "LIMIT" in sql
        assert "WHERE" in sql and "ORDER BY" in sql
        if scope == "watchlist":
            assert "JOIN" in sql


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


@pytest.mark.asyncio
async def test_export_uses_single_stream_no_count():
    rows = [_Row(f"N{i}", f"S{i:05d}") for i in range(10)]
    db = _FakeDb(rows=rows)
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value="h")), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())), \
         patch.object(mes, "release_lock", new=AsyncMock()):
        prepared = await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    try:
        assert len(db.stream_calls) == 1          # 恰好一次流式 SELECT
        assert db.scalar_calls == []          # 无 count 查询
        assert prepared.rows == 10
    finally:
        shutil.rmtree(prepared.tmp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_export_5000_one_stream_valid_xlsx():
    rows = [_Row(f"N{i}", f"S{i:05d}") for i in range(5000)]
    db = _FakeDb(rows=rows)
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value="h")), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())), \
         patch.object(mes, "release_lock", new=AsyncMock()):
        prepared = await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    try:
        assert len(db.stream_calls) == 1
        assert db.last_result.partition_count == 20
        assert db.last_result.closed is True
        assert prepared.rows == 5000
        assert prepared.batches == 20
        assert prepared.max_batch == mes.EXPORT_BATCH_SIZE
        zf = zipfile.ZipFile(prepared.final_path)
        assert zf.testzip() is None
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert sheet.count('<row r="') == 5001
    finally:
        shutil.rmtree(prepared.tmp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_export_over_10001_returns_422_and_cleans_up():
    rows = [_Row(f"N{i}", f"S{i:05d}") for i in range(mes.MAX_EXPORT_ROWS + 1)]
    db = _FakeDb(rows=rows)
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    holder = "holder-x"
    released = []
    captured = {}
    _real_mkdtemp = tempfile.mkdtemp

    def _fake_mkdtemp(prefix=None, **k):
        d = _real_mkdtemp(prefix=prefix, **k)
        captured["path"] = d
        return d

    with patch.object(tempfile, "mkdtemp", _fake_mkdtemp), \
         patch.object(mes, "acquire_lock", new=AsyncMock(return_value=holder)), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())), \
         patch.object(mes, "release_lock", new=AsyncMock(side_effect=lambda *a, **k: released.append(a))):
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    assert ei.value.status_code == 422
    assert len(db.stream_calls) == 1
    assert db.last_result.closed is True
    assert released and released[0] == (mes.EXPORT_LOCK_KEY, holder)
    assert not os.path.exists(captured["path"])  # 超限即清理临时目录


@pytest.mark.asyncio
async def test_export_stream_exception_cleans_up():
    class _BoomResult:
        def partitions(self, size=None):
            async def _gen():
                yield [_Row("x", "1")]
                raise RuntimeError("db gone")
            return _gen()

        async def close(self):
            self.closed = True

    db = _FakeDb(result=_BoomResult())
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    holder = "holder-y"
    released = []
    captured = {}
    _real_mkdtemp = tempfile.mkdtemp

    def _fake_mkdtemp(prefix=None, **k):
        d = _real_mkdtemp(prefix=prefix, **k)
        captured["path"] = d
        return d

    with patch.object(tempfile, "mkdtemp", _fake_mkdtemp), \
         patch.object(mes, "acquire_lock", new=AsyncMock(return_value=holder)), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())), \
         patch.object(mes, "release_lock", new=AsyncMock(side_effect=lambda *a, **k: released.append(a))):
        with pytest.raises(RuntimeError):
            await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    assert db.last_result.closed is True
    assert released and released[0] == (mes.EXPORT_LOCK_KEY, holder)
    assert not os.path.exists(captured["path"])


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


@pytest.mark.asyncio
async def test_preflight_busy_before_db_returns_429():
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value=None)), \
         patch.object(mes, "_assemble_market_query", new=AsyncMock()) as mock_assemble, \
         patch.object(mes, "MarketXlsxWriter", new=MagicMock()) as mock_writer_cls, \
         patch.object(mes, "release_lock", new=AsyncMock()) as mock_release:
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(None, plan, uuid.UUID(int=0))
    assert ei.value.status_code == 429
    # 锁竞争失败（429）必须发生在任何重 DB 工作之前
    assert mock_assemble.call_count == 0
    assert mock_writer_cls.call_count == 0
    # 未获得锁，不应执行释放
    assert mock_release.call_count == 0


@pytest.mark.asyncio
async def test_preflight_over_limit_returns_422_and_releases_lock():
    # 流式读到第 10001 行即 422（不再依赖 count 查询）
    rows = [_Row(f"N{i}", f"S{i:05d}") for i in range(mes.MAX_EXPORT_ROWS + 1)]
    db = _FakeDb(rows=rows)
    plan = mes.build_export_plan(MarketExportRequest(**_body()))
    holder = "holder-x"
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value=holder)) as mock_acquire, \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())) as mock_assemble, \
         patch.object(mes, "MarketXlsxWriter", new=MagicMock()) as mock_writer_cls, \
         patch.object(mes, "release_lock", new=AsyncMock()) as mock_release:
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(db, plan, uuid.UUID(int=0))
    assert ei.value.status_code == 422
    assert mock_acquire.call_count == 1
    assert mock_assemble.call_count == 1
    assert len(db.stream_calls) == 1
    # 超限路径绝不 finalize/build_zip（writer 构造本身发生 1 次，正常）
    assert mock_writer_cls.call_count == 1
    mock_writer_cls.return_value.build_zip.assert_not_called()
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
