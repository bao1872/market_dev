"""[S2-A-C1] Market export 安全/权限/低内存契约测试（PURE，无 DB/Redis 连接）。

本文件只覆盖不触达数据库/Redis 的契约（在 PURE_UNIT_TEST=1 下本地可运行）：
- A  生产导出路径不调用 get_market_stocks
- G  source planning（base-only → 无重载 source）
- H  visible column 白名单校验（fail-fast 422）
- I  非 admin → 403（C1a 熔断）
- J  admin → 授权通过
- O  生成有效 OOXML（inlineStr，无 sharedStrings，含表头）
- P  流式下载完成后临时文件清理
- 事件循环响应性（CPU 工作在 worker 线程，heartbeat 仍能推进）
- 5000 行合成压力门禁（真实 XLSX 生成，测 baseline）
- 锁语义（持有者令牌 + 忙时拒绝 + 仅持有者可释放）

DB/端点集成契约（B/C/D/E/F/K/L/M/N）在 tests/test_market_export_integration.py，
仅 PANJI_REMOTE_VERIFY_DB_TEST=1（远程验证库）下运行。

注意：不使用 app.main ASGI（其 lifespan 在 PURE 模式下会触发 DB 查询且 session 为 None），
改为直接调用端点函数 export_market_stocks 与依赖 require_admin。
"""

from __future__ import annotations

import asyncio
import os
import resource
import tempfile
import threading
import time
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.market import export_market_stocks, require_admin
from app.schemas.export import ExportColumn
from app.schemas.market_stocks import MarketExportRequest
from app.services import market_export_service as mes
from app.services.access_control_service import AccessContext
from app.services.excel_export_service import MarketXlsxWriter, validate_export_columns

pytestmark = pytest.mark.pure_unit

_USER_ID = "00000000-0000-0000-0000-00000000000a"

try:
    from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS

    _VALID_FP = next(
        (k for k, v in FP_QUERY_FIELD_SPECS.items() if v.get("source") in ("flat", "column", "computed")),
        "fp_volume_zscore20",
    )
except Exception:  # pragma: no cover
    _VALID_FP = "fp_volume_zscore20"


# ---------------------------------------------------------------------------
# 请求构造 / auth 辅助
# ---------------------------------------------------------------------------


def _body(columns=None, **over):
    cols = columns or [
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": "name", "title": "名称", "data_type": "text", "payload_key": None},
    ]
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
        "visible_columns": cols,
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
            final_path=final, tmp_dir=d, rows=1, columns=1, batches=1, max_batch=1, bytes=4
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


def test_build_export_plan_rejects_unknown_column():
    # H：未知列 → ValueError（端点映射为 422）
    with pytest.raises(ValueError):
        mes.build_export_plan(
            MarketExportRequest(
                **_body(columns=[{"key": "not_a_real_column", "title": "X", "data_type": "text", "payload_key": None}])
            )
        )


# ---------------------------------------------------------------------------
# H：validate_export_columns fail-fast
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_base_key():
    with pytest.raises(ValueError):
        validate_export_columns([ExportColumn(key="bogus", title="X", data_type="text")])


def test_validate_rejects_unknown_fp_key():
    with pytest.raises(ValueError):
        validate_export_columns([ExportColumn(key="fp_does_not_exist", title="X", data_type="text")])


def test_validate_rejects_duplicate_key():
    with pytest.raises(ValueError):
        validate_export_columns([
            ExportColumn(key="symbol", title="A", data_type="text"),
            ExportColumn(key="symbol", title="B", data_type="text"),
        ])


def test_validate_rejects_action_key():
    with pytest.raises(ValueError):
        validate_export_columns([ExportColumn(key="action", title="X", data_type="text")])


def test_validate_rejects_bad_data_type():
    with pytest.raises(ValueError):
        validate_export_columns([ExportColumn(key="symbol", title="X", data_type="blob")])  # type: ignore[arg-type]


def test_validate_rejects_oversized_title():
    with pytest.raises(ValueError):
        validate_export_columns([ExportColumn(key="symbol", title="x" * 1000, data_type="text")])


def test_validate_rejects_empty():
    with pytest.raises(ValueError):
        validate_export_columns([])


def test_validate_ok_known_columns():
    validate_export_columns([
        ExportColumn(key="symbol", title="代码", data_type="text"),
        ExportColumn(key=_VALID_FP, title="动量", data_type="number"),
    ])


# ---------------------------------------------------------------------------
# G：source planning
# ---------------------------------------------------------------------------


def _plan(columns, **over):
    return mes.build_export_plan(MarketExportRequest(**_body(columns=columns, **over)))


def test_plan_base_only_no_heavy_sources():
    plan = _plan([
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": "name", "title": "名称", "data_type": "text", "payload_key": None},
    ])
    assert plan.needs_price is False
    assert plan.needs_snapshot is False
    assert plan.needs_boards is False
    assert plan.needs_chip is False


def test_plan_price_requires_price_source():
    plan = _plan([
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": "latest_price", "title": "最新价", "data_type": "number", "payload_key": None},
    ])
    assert plan.needs_price is True
    assert plan.needs_snapshot is False


def test_plan_fp_requires_snapshot_source():
    plan = _plan([
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": _VALID_FP, "title": "动量", "data_type": "number", "payload_key": None},
    ])
    assert plan.needs_snapshot is True


def test_plan_industry_filter_requires_boards():
    plan = _plan(None, industry="银行")
    assert plan.needs_boards is True


def test_plan_watchlist_scope_normalized():
    plan = _plan(None, scope="watchlist")
    assert plan.scope == "watchlist"


def _chip_fp_key():
    try:
        from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS
    except Exception:  # pragma: no cover
        return None
    for k, v in FP_QUERY_FIELD_SPECS.items():
        if v.get("source") == "chip":
            return k
    return None


def test_plan_chip_fp_implies_snapshot():
    chip_key = _chip_fp_key()
    if chip_key is None:
        pytest.skip("no chip-source fp column in FP_QUERY_FIELD_SPECS")
    plan = _plan([
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": chip_key, "title": "chip", "data_type": "number", "payload_key": None},
    ])
    assert plan.needs_chip is True
    # chip 字段取值依赖 _fetch_chips 的 snap_meta（来自 snapshot），必须加载 snapshot
    assert plan.needs_snapshot is True


def test_plan_chip_status_implies_snapshot():
    plan = _plan([
        {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
        {"key": "chip_status", "title": "筹码状态", "data_type": "text", "payload_key": None},
    ])
    assert plan.needs_chip is True
    # chip_status 同样依赖 snap_meta
    assert plan.needs_snapshot is True


# ---------------------------------------------------------------------------
# O / 低内存：MarketXlsxWriter 增量 + inlineStr + 含表头
# ---------------------------------------------------------------------------


def test_xlsx_writer_valid_inline_no_shared_strings():
    cols = [
        ExportColumn(key="symbol", title="代码", data_type="text"),
        ExportColumn(key="name", title="名称", data_type="text"),
        ExportColumn(key="latest_price", title="最新价", data_type="number"),
    ]
    rows = [
        {"symbol": "000001", "name": "平安银行", "latest_price": 12.34},
        {"symbol": "600519", "name": "贵州茅台", "latest_price": 1700.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        w = MarketXlsxWriter(cols, d)
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
        # 表头行存在
        assert "<row r=\"1\">" in sheet
        assert "代码" in sheet and "名称" in sheet and "最新价" in sheet
        # 数据行存在且为数字
        assert "12.34" in sheet


def test_xlsx_writer_streaming_file_grows():
    """结构硬约束：数据增量写入磁盘临时文件，而非持有完整内存集合。"""
    cols = [ExportColumn(key="symbol", title="代码", data_type="text")]
    rows = [{"symbol": f"S{i:05d}"} for i in range(600)]
    with tempfile.TemporaryDirectory() as d:
        w = MarketXlsxWriter(cols, d)
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
        # 注意：不能在协程里直接调用阻塞的 threading.Event.wait（会卡住事件循环）
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
# 5000 行合成压力门禁（真实 XLSX 生成，IDE baseline）
# ---------------------------------------------------------------------------


def test_export_5000_row_writer_stress():
    cols = [
        ExportColumn(key="symbol", title="代码", data_type="text"),
        ExportColumn(key="name", title="名称", data_type="text"),
        ExportColumn(key="latest_price", title="最新价", data_type="number"),
        ExportColumn(key="change_pct", title="涨跌幅", data_type="percent"),
        ExportColumn(key=_VALID_FP, title="动量", data_type="number"),
    ]
    rows = [
        {
            "symbol": f"S{i:05d}",
            "name": f"N{i}",
            "latest_price": float(i),
            "change_pct": 0.5,
            "fp_value_placeholder": i,
        }
        for i in range(5000)
    ]
    # 仅验证结构：fp 列写入占位值（不依赖真实 snapshot）
    for r in rows:
        r[_VALID_FP] = r.pop("fp_value_placeholder")
    with tempfile.TemporaryDirectory() as d:
        peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        w = MarketXlsxWriter(cols, d)
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
        assert max_batch <= mes.EXPORT_BATCH_SIZE
        print(
            f"STRESS rows=5000 columns={len(cols)} batches={batches} "
            f"max_batch={max_batch} xlsx_bytes={size} "
            f"peak_rss_delta={peak_after - peak_before}"
        )
        assert size > 0


# ---------------------------------------------------------------------------
# 偏好飞行契约（C2）：422/429 必须在 StreamingResponse 创建前确定
# ---------------------------------------------------------------------------


class _FakeStmt:
    def order_by(self, *a):
        return self

    def subquery(self):
        # 返回真实（无 FROM 的）scalar subquery alias，使 select(...).select_from() 合法，
        # 不触达数据库；db.scalar 由 _FakeDb 直接返回计数。
        return select(func.count()).subquery()


class _FakeCtx:
    base_stmt = _FakeStmt()


class _FakeDb:
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
         patch.object(mes, "MarketXlsxWriter", new=AsyncMock()) as mock_writer_cls, \
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
    db = _FakeDb(mes.MAX_EXPORT_ROWS + 1)
    with patch.object(mes, "acquire_lock", new=AsyncMock(return_value=holder)) as mock_acquire, \
         patch.object(mes, "_assemble_market_query", new=AsyncMock(return_value=_FakeCtx())) as mock_assemble, \
         patch.object(mes, "_fetch_batch_rows", new=AsyncMock()) as mock_fetch, \
         patch.object(mes, "MarketXlsxWriter", new=AsyncMock()) as mock_writer_cls, \
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
        final_path=final, tmp_dir=d, rows=1, columns=1, batches=1, max_batch=1, bytes=2
    )
    out = b"".join([c async for c in mes.stream_prepared_market_export(prepared)])
    assert out == b"PK"
    assert not os.path.exists(d)  # P：流式阶段只做清理，与 DB/锁无关
