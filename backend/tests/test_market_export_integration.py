"""[S2-A-C1] Market export DB/集成契约测试（仅 PANJI_REMOTE_VERIFY_DB_TEST=1 运行）。

本地 IDE 不连数据库，故本文件在 PURE_UNIT_TEST=1 / 普通环境下整体 skip。
覆盖 DB 触达的资源路径契约：

- B   filtered count > MAX_EXPORT_ROWS → 422，且绝不进入分批 fetch / XLSX writer
- C   5000 行：batch <= EXPORT_BATCH_SIZE、batches=20、max_batch=250、无重复/遗漏
- G   source planning 在 DB 路径下生效（base-only 不拉 snapshot）
- K/L 全局租约忙时 → 429（pre-held lock）
- M/N query/writer 异常 → 租约释放（无泄漏）
- fp  导出真实读取 first_pyramid_flat（证明 export 复用列表 flatten 路径）

D/E/F（fp_filter/fp_sort 语义正确性）由列表路径的 PG 集成测试守护：
export 与 list 共用 _parse_fp_filter / _parse_fp_sort / _needs_snap_lateral /
_build_fp_filter_conditions 同一套单一真源 helper，本文件不再重复断言其语义，
只断言 export 触发了正确的 source loading。
"""

from __future__ import annotations

import os
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.schemas.market_stocks import MarketExportRequest
from app.services import market_export_service as mes
from app.services.first_pyramid_flatten import flatten_first_pyramid
from app.services.market_stocks_service import _SCHEMA_VERSION

_REMOTE = os.environ.get("PANJI_REMOTE_VERIFY_DB_TEST", "").lower() in ("1", "true", "yes")
requires_verify = pytest.mark.skipif(
    not _REMOTE, reason="needs PANJI_REMOTE_VERIFY_DB_TEST=1 (remote verify DB)"
)
pytestmark = requires_verify

_USER_ID = "00000000-0000-0000-0000-00000000000a"


def _body(columns, **over):
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
        "visible_columns": columns,
    }
    b.update(over)
    return b


async def _seed_instruments(db: AsyncSession, n: int, prefix: str = "EXP") -> list[Instrument]:
    objs = [
        Instrument(symbol=f"{prefix}{i:06d}", name=f"标的{i}", market="A")
        for i in range(n)
    ]
    db.add_all(objs)
    await db.commit()
    return objs


async def _seed_snapshots(db: AsyncSession, objs: list[Instrument], fp_raw: dict) -> None:
    snaps = []
    for inst in objs:
        flat = flatten_first_pyramid(fp_raw)
        snaps.append(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date="2026-09-15",
                source_run_id="00000000-0000-0000-0000-0000000000ff",
                algorithm_version="test",
                schema_version=_SCHEMA_VERSION,
                summary_payload={"first_pyramid": fp_raw, "first_pyramid_flat": flat},
                status="ready",
            )
        )
    db.add_all(snaps)
    await db.commit()


# ---------------------------------------------------------------------------
# B：over-limit 422 + 不进入分批/writer
# ---------------------------------------------------------------------------


async def test_over_limit_returns_422_and_no_batch_fetch(db_session: AsyncSession):
    await _seed_instruments(db_session, mes.MAX_EXPORT_ROWS + 1)
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([{"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None}])
        )
    )
    calls = {"batches": 0}

    async def _fake_fetch(*a, **k):
        calls["batches"] += 1
        return []

    with pytest.raises(HTTPException) as ei, _Patch(mes, "_fetch_batch_rows", _fake_fetch):
        await mes._build_export_file(db_session, plan, __import__("uuid").UUID(_USER_ID))
    assert ei.value.status_code == 422
    assert calls["batches"] == 0  # 超限即拒，绝不分批 fetch / writer


# ---------------------------------------------------------------------------
# C：5000 行有界分批 + 完整无重复/遗漏
# ---------------------------------------------------------------------------


async def test_5000_rows_bounded_batches_and_complete(db_session: AsyncSession):
    await _seed_instruments(db_session, 5000)
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([{"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None}])
        )
    )
    final_path, stats = await mes._build_export_file(
        db_session, plan, __import__("uuid").UUID(_USER_ID)
    )
    try:
        assert stats["rows"] == 5000
        assert stats["batches"] == (5000 + mes.EXPORT_BATCH_SIZE - 1) // mes.EXPORT_BATCH_SIZE
        assert stats["max_batch"] <= mes.EXPORT_BATCH_SIZE
        zf = zipfile.ZipFile(final_path)
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        # 表头 + 5000 数据行 = 5001 个 <row r="
        assert sheet.count('<row r="') == 5001
    finally:
        import shutil

        shutil.rmtree(os.path.dirname(final_path), ignore_errors=True)


# ---------------------------------------------------------------------------
# G：base-only 在 DB 路径不加载 snapshot
# ---------------------------------------------------------------------------


async def test_base_only_no_snapshot_fetch(db_session: AsyncSession):
    await _seed_instruments(db_session, 3)
    # 仅 base 列，无 snapshot/board/chip 需求
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([{"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None}])
        )
    )
    assert plan.needs_snapshot is False
    assert plan.needs_boards is False
    assert plan.needs_chip is False
    ctx = await mes._assemble_market_query(
        db_session, __import__("uuid").UUID(_USER_ID),
        plan.scope, plan.query, plan.state, plan.industry, plan.concept,
        plan.fp_filter, plan.fp_sort, plan.sort,
    )
    # base-only 不应构建 snapshot/chip LATERAL
    assert ctx.needs_snap is False
    assert ctx.needs_chip is False
    rows = await mes._fetch_batch_rows(db_session, ctx, plan, 0)
    assert len(rows) == 3
    assert all(isinstance(r.get("symbol"), str) for r in rows)


# ---------------------------------------------------------------------------
# fp：导出真实读取 first_pyramid_flat
# ---------------------------------------------------------------------------


async def test_fp_columns_read_from_snapshot(db_session: AsyncSession):
    objs = await _seed_instruments(db_session, 2)
    await _seed_snapshots(db_session, objs, {"trendDirection": "上行", "trendBars": 5})
    fp_key = next(
        k for k, v in __import__("app.services.first_pyramid_flatten", fromlist=["FP_QUERY_FIELD_SPECS"]).FP_QUERY_FIELD_SPECS.items()
        if v.get("source") in ("flat", "column", "computed")
    )
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([
                {"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None},
                {"key": fp_key, "title": "fp", "data_type": "number", "payload_key": None},
            ])
        )
    )
    assert plan.needs_snapshot is True
    ctx = await mes._assemble_market_query(
        db_session, __import__("uuid").UUID(_USER_ID),
        plan.scope, plan.query, plan.state, plan.industry, plan.concept,
        plan.fp_filter, plan.fp_sort, plan.sort,
    )
    rows = await mes._fetch_batch_rows(db_session, ctx, plan, 0)
    assert len(rows) == 2
    # 至少一只股票读到了 fp_* 字段（export 复用列表 flatten 路径）
    assert any(k.startswith("fp_") for r in rows for k in r.keys())


# ---------------------------------------------------------------------------
# K/L：全局租约忙时 → 429
# ---------------------------------------------------------------------------


async def test_concurrent_export_busy_returns_429(db_session: AsyncSession):
    await _seed_instruments(db_session, 5)
    # 预先持有全局租约
    holder = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 600, "held-by-other")
    assert holder is not None
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([{"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None}])
        )
    )
    try:
        with pytest.raises(HTTPException) as ei:
            await mes._build_export_file(db_session, plan, __import__("uuid").UUID(_USER_ID))
        assert ei.value.status_code == 429
    finally:
        await mes.release_lock(mes.EXPORT_LOCK_KEY, "held-by-other")


# ---------------------------------------------------------------------------
# N：writer 异常 → 租约释放（无泄漏）
# ---------------------------------------------------------------------------


async def test_writer_exception_releases_lock(db_session: AsyncSession):
    await _seed_instruments(db_session, 5)
    plan = mes.build_export_plan(
        MarketExportRequest(
            **_body([{"key": "symbol", "title": "代码", "data_type": "text", "payload_key": None}])
        )
    )

    async def _boom(self, *a, **k):
        raise RuntimeError("simulated writer failure")

    with pytest.raises(RuntimeError), _Patch(mes.MarketXlsxWriter, "build_zip", _boom):
        await mes._build_export_file(db_session, plan, __import__("uuid").UUID(_USER_ID))
    # 锁应已被释放：可再次获取
    holder = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 600, "reacquire")
    assert holder is not None
    await mes.release_lock(mes.EXPORT_LOCK_KEY, "reacquire")


# ---------------------------------------------------------------------------
# 小工具：pytest.MonkeyPatch 兼容包装（覆盖模块/类属性）
# ---------------------------------------------------------------------------


class _Patch:
    """极简 monkeypatch 上下文（避免不同 pytest 版本 API 差异）。"""

    def __init__(self, target, attr, replacement):
        self._target = target
        self._attr = attr
        self._replacement = replacement
        self._orig = None

    def __enter__(self):
        self._orig = getattr(self._target, self._attr)
        setattr(self._target, self._attr, self._replacement)
        return self

    def __exit__(self, *exc):
        setattr(self._target, self._attr, self._orig)
        return False
