"""[S2-A-C1] Market export DB/集成契约测试（仅 PANJI_REMOTE_VERIFY_DB_TEST=1 运行）。

本地 IDE 不连数据库，故本文件在 PURE_UNIT_TEST=1 / 普通环境下整体 skip。
覆盖 DB 触达的资源路径契约：

- B   LIMIT(MAX_EXPORT_ROWS+1) 流式读到第 10001 行 → 422，绝不生成终态 XLSX（无 OFFSET 分页 / 无 count）
- C   5000 行：1 次流式 SELECT、batches=20、max_batch<=250、无重复/遗漏、固定 2 列
- G   base-only 在 DB 路径不加载 snapshot（仅筛选/排序需要的 source 才会被 _assemble_market_query 使用）
- K/L 全局租约忙时 → 429（pre-held lock）
- M/N query/writer 异常 → 租约释放（无泄漏）

导出列由服务端固定为「股票名称 + 股票代码」，不再有任何 display-field enrichment；
导出服务也不再调用 _fetch_prices / _fetch_snapshots / _fetch_chips。
筛选/排序语义正确性由列表路径的 PG 集成测试守护（export 与 list 共用同一套
_parse_fp_filter / _parse_fp_sort / _needs_snap_lateral / _build_fp_filter_conditions）。

[C4-R1] fixture 必须命中生产 universe 口径（否则契约退化为空断言）：
  导出/列表的标的集合由 `_build_search_conditions` 无条件叠加的
  `instrument_maintenance_service.stock_symbol_sql_filter` 决定 ——
  只保留 market ∈ {SH,SZ,BJ} 且 symbol 为真实 6 位 A 股代码的标的。
  旧 fixture（`symbol="EXP%06d"` + `market="A"`）整体被该过滤器排除，
  导出只能看到库里其他测试残留的真实标的，于是
  「rows == 5000 / rows == 3」变成假失败、超限 422 也永不触发（测试零覆盖）。
  现在：真实代码 + fixture sanity 断言 + universe 可见性断言（见 _seed_instruments）。

  另一个共享库副作用：verify 库全 suite 复用，其他测试也会写入真实标的，
  故每个用例用**独立 name marker**（stock_name / keyword 过滤）隔离自己的
  universe 段，精确断言行数才有意义；同时避免前序用例播种的 >10000 行
  把后续用例推入 422 分支。
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.schemas.market_stocks import MarketExportRequest
from app.services import market_export_service as mes
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

_REMOTE = os.environ.get("PANJI_REMOTE_VERIFY_DB_TEST", "").lower() in ("1", "true", "yes")
requires_verify = pytest.mark.skipif(
    not _REMOTE, reason="needs PANJI_REMOTE_VERIFY_DB_TEST=1 (remote verify DB)"
)
pytestmark = requires_verify

_USER_ID = "00000000-0000-0000-0000-00000000000a"

# [C4-R1] 每个用例独占一个「真实代码段 + name marker」，互不包含子串。
#   - 代码段：6 位真实 A 股代码（market="SH"，^6\d{5}$），命中生产 universe；
#   - name marker：把该用例的标的从共享 verify 库的其他真实标的里隔离出来。
_MARK_OVER_LIMIT = "C4R1OVERLIMIT"
_MARK_ROWS_5000 = "C4R1ROWS5000"
_MARK_BASE_ONLY = "C4R1BASEONLY"
_MARK_BUSY = "C4R1BUSYLOCK"
_MARK_WRITER = "C4R1WRITEREXC"
_MARK_QUERY = "C4R1QUERYEXC"


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


async def _seed_instruments(
    db: AsyncSession, n: int, *, base_symbol: int, name_marker: str
) -> list[Instrument]:
    """[C4-R1] 播种 n 条**真实 A 股口径**标的（market="SH" + 6 位数字代码）。

    旧 fixture 用 `symbol="EXP%06d" + market="A"`，被生产 universe 口径
    `stock_symbol_sql_filter` 整体排除，导出查询看不到任何 fixture 标的，
    断言退化为空断言（只看到库里别家残留标的）。此处同时保证三件事：

    1. fixture sanity：market ∈ {SH,SZ,BJ} 且 symbol 为 6 位数字代码，
       不满足就地失败（禁止再出现空断言 fixture）；
    2. 符号唯一；
    3. universe sanity：按生产过滤器实际计数，证明这批标的**真实可见**。
    """
    objs = [
        Instrument(
            symbol=f"{base_symbol + i:06d}", name=f"{name_marker}{i:06d}", market="SH"
        )
        for i in range(n)
    ]
    symbols = [o.symbol for o in objs]
    assert len(set(symbols)) == n, "fixture symbol 必须唯一"
    for o in objs:
        assert o.market in {"SH", "SZ", "BJ"}, f"market 不在 A 股市场：{o.market}"
        assert re.fullmatch(r"\d{6}", o.symbol), f"symbol 非 6 位数字代码：{o.symbol}"
    db.add_all(objs)
    await db.commit()
    # universe sanity：用生产过滤器证明 fixture 命中（而不是被静默过滤掉）
    visible = await db.scalar(
        select(func.count())
        .select_from(Instrument)
        .where(
            Instrument.name.ilike(f"%{name_marker}%"),
            stock_symbol_sql_filter(Instrument),
        )
    )
    assert visible == n, f"fixture 未命中生产 universe：visible={visible} expected={n}"
    return objs


# ---------------------------------------------------------------------------
# B：over-limit 422 + 不进入分批/writer
# ---------------------------------------------------------------------------


async def test_over_limit_returns_422_and_no_final_xlsx(db_session: AsyncSession):
    # [C4-R1] 播种 MAX_EXPORT_ROWS + 1 = 10001 条**真实可见**标的。
    # 旧 fixture（EXP + market="A"）被生产 universe 过滤器整体排除，本用例从未真正进入超限分支。
    await _seed_instruments(
        db_session,
        mes.MAX_EXPORT_ROWS + 1,
        base_symbol=605000,
        name_marker=_MARK_OVER_LIMIT,
    )
    plan = mes.build_export_plan(MarketExportRequest(**_body(stock_name=_MARK_OVER_LIMIT)))
    zip_calls = {"n": 0}

    def _no_zip(self, *a, **k):
        zip_calls["n"] += 1

    with pytest.raises(HTTPException) as ei, _Patch(mes.MarketXlsxWriter, "build_zip", _no_zip):
        await mes.prepare_market_export(db_session, plan, uuid.UUID(_USER_ID))
    assert ei.value.status_code == 422
    # 流式读到第 10001 行即 422：绝不生成终态 XLSX（finalize/build_zip 不执行）
    assert zip_calls["n"] == 0


# ---------------------------------------------------------------------------
# C：5000 行有界分批 + 完整无重复/遗漏 + 固定 2 列
# ---------------------------------------------------------------------------


async def test_5000_rows_bounded_batches_and_complete(db_session: AsyncSession):
    rows_n = 5000
    seed_base = 600000  # 600000..604999：真实 A 股代码段且符号唯一
    await _seed_instruments(
        db_session, rows_n, base_symbol=seed_base, name_marker=_MARK_ROWS_5000
    )
    plan = mes.build_export_plan(MarketExportRequest(**_body(stock_name=_MARK_ROWS_5000)))
    prepared = await mes.prepare_market_export(db_session, plan, uuid.UUID(_USER_ID))
    try:
        # export universe sanity：本用例自己的标的段必须被完整导出（不多不少）
        assert prepared.rows == rows_n
        assert prepared.columns == 2
        assert prepared.batches == (rows_n + mes.EXPORT_BATCH_SIZE - 1) // mes.EXPORT_BATCH_SIZE
        assert prepared.max_batch <= mes.EXPORT_BATCH_SIZE
        zf = zipfile.ZipFile(prepared.final_path)
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        # 表头 + rows_n 数据行
        assert sheet.count('<row r="') == rows_n + 1
        # 仅两列
        header = sheet.split('<row r="1">')[1].split("</row>")[0]
        assert header.count("<c ") == 2

        # 从第二列（symbol，列 B）提取全部 symbol，验证无重复 / 无遗漏（C3a）
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        root = ET.fromstring(sheet)
        symbols: list[str] = []
        for row in root.iter(f"{ns}row"):
            if row.get("r") == "1":
                continue  # 跳过表头
            for c in row.findall(f"{ns}c"):
                ref = c.get("r", "")
                if ref and ref[0].upper() == "B":
                    t = c.find(f"{ns}is/{ns}t")
                    if t is not None and t.text is not None:
                        symbols.append(t.text)
        expected = {f"{seed_base + i:06d}" for i in range(rows_n)}
        assert len(symbols) == rows_n        # 数量完整（无遗漏）
        assert len(set(symbols)) == rows_n   # 无重复
        assert set(symbols) == expected      # 集合完全一致（无错配 / 无遗漏）
    finally:
        shutil.rmtree(prepared.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# G：base-only 在 DB 路径不加载 snapshot
# ---------------------------------------------------------------------------


async def test_base_only_no_snapshot_fetch(db_session: AsyncSession):
    seed_base = 688001  # 科创板真实代码段
    await _seed_instruments(
        db_session, 3, base_symbol=seed_base, name_marker=_MARK_BASE_ONLY
    )
    # 仅基础筛选；keyword 同时把本用例标的段从共享 verify 库隔离出来，
    # 使 len(rows) 精确断言有意义（库中还有别家真实标的）。
    plan = mes.build_export_plan(MarketExportRequest(**_body(keyword=_MARK_BASE_ONLY)))
    ctx = await mes._assemble_market_query(
        db_session, uuid.UUID(_USER_ID),
        plan.scope, plan.query, plan.state, plan.industry, plan.concept,
        plan.fp_filter, plan.fp_sort, plan.sort,
    )
    # base-only 不应构建 snapshot/chip LATERAL
    assert ctx.needs_snap is False
    assert ctx.needs_chip is False
    stmt = mes._build_export_rows_stmt(ctx)
    result = await db_session.stream(stmt)
    rows = []
    async for partition in result.partitions(mes.EXPORT_BATCH_SIZE):
        rows.extend({"name": r.name, "symbol": r.symbol} for r in partition)
    await result.close()
    assert len(rows) == 3
    assert {r["symbol"] for r in rows} == {f"{seed_base + i:06d}" for i in range(3)}
    for r in rows:
        assert set(r.keys()) == {"name", "symbol"}


# ---------------------------------------------------------------------------
# K/L：全局租约忙时 → 429
# ---------------------------------------------------------------------------


async def test_concurrent_export_busy_returns_429(db_session: AsyncSession):
    await _seed_instruments(db_session, 5, base_symbol=688010, name_marker=_MARK_BUSY)
    # 预先持有全局租约
    holder = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 600, "held-by-other")
    assert holder is not None
    plan = mes.build_export_plan(MarketExportRequest(**_body(stock_name=_MARK_BUSY)))
    try:
        with pytest.raises(HTTPException) as ei:
            await mes.prepare_market_export(db_session, plan, uuid.UUID(_USER_ID))
        assert ei.value.status_code == 429
    finally:
        await mes.release_lock(mes.EXPORT_LOCK_KEY, "held-by-other")


# ---------------------------------------------------------------------------
# N：writer 异常 → 租约释放（无泄漏）
# ---------------------------------------------------------------------------


async def test_writer_exception_releases_lock(db_session: AsyncSession):
    await _seed_instruments(db_session, 5, base_symbol=688020, name_marker=_MARK_WRITER)
    # [C4-R1] 必须隔离本用例 universe：共享 verify 库中前序用例已累积 >10000 行真实标的，
    # 不隔离会先命中 422 超限分支，永远走不到 build_zip（writer 异常路径零覆盖）。
    plan = mes.build_export_plan(MarketExportRequest(**_body(stock_name=_MARK_WRITER)))

    def _boom(self, *a, **k):
        raise RuntimeError("simulated writer failure")

    with pytest.raises(RuntimeError), _Patch(mes.MarketXlsxWriter, "build_zip", _boom):
        await mes.prepare_market_export(db_session, plan, uuid.UUID(_USER_ID))
    # 锁应已被释放：可再次获取
    holder = await mes.acquire_lock(mes.EXPORT_LOCK_KEY, 600, "reacquire")
    assert holder is not None
    await mes.release_lock(mes.EXPORT_LOCK_KEY, "reacquire")


# ---------------------------------------------------------------------------
# M：query 异常 → 租约释放（锁已提前到 query 之前，此路径必要）
# ---------------------------------------------------------------------------


async def test_query_exception_releases_lock(db_session: AsyncSession):
    await _seed_instruments(db_session, 5, base_symbol=688030, name_marker=_MARK_QUERY)
    plan = mes.build_export_plan(MarketExportRequest(**_body(stock_name=_MARK_QUERY)))

    def _boom_assemble(*a, **k):
        raise RuntimeError("simulated query failure")

    with pytest.raises(RuntimeError), _Patch(mes, "_assemble_market_query", _boom_assemble):
        await mes.prepare_market_export(db_session, plan, uuid.UUID(_USER_ID))
    # 锁应已被释放：可再次获取（无 temp 泄漏）
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
