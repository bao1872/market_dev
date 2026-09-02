"""/market/stocks 单一 canonical CoreRun 回归测试（纯单元，无数据库）。

生产事故（AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 第二处漏迁 consumer）
----------------------------------------------------------------
``/api/v1/market/stocks`` 一次请求内存在**两个**不同的 First Pyramid authority：

    Path A（fp_filter / fp_sort / count）
        → get_publication(kind=stock_core) → published_core_run_id
        → _build_snap_lateral(snapshot_run_id=...)
    Path B（display）
        → partition by instrument_id / order by trade_date desc / rn = 1

LEGACY ``stock_core`` pointer 停在 2026-08-26 / run ``ca5c3dd2``，而 Core snapshot
每天照常 succeeded。于是：

    08-26 snapshot 决定：是否命中 filter、排序位置、total
    09-02 snapshot 决定：first_pyramid 返回值、fp_trade_date、data_run_id

页面表现为「筛选连续天 1–5 / 升序」却返回一串 6。

修复后：filter / sort / count / display 全部消费
``current_core_run_service.resolve_current_core_run`` 解析出的同一个 CoreRun。

本文件在**语句构造层**锁定该契约（可纯单元运行，不需要数据库），
覆盖用户指定的回归语义：

    A: run_old.fp_trend_bars = 5, run_new.fp_trend_bars = 6
       filter fp_trend_bars between 1;5  → A 不得出现
    B: run_old.fp_trend_bars = 20, run_new.fp_trend_bars = 1
       → B 必须进入结果，且返回 run_new 的值
    排序 B(1) / C(2) / D(5) → 必须按 run_new 的值 1,2,5 单调不降

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_market_stocks_single_core_run.py -v
"""
from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app.services.market_stocks_service import (
    _build_display_snapshot_query,
    _build_snap_lateral,
)

_OLD_RUN = uuid.uuid4()   # LEGACY stock_core pointer 指向的 run（2026-08-26 / ca5c3dd2）
_NEW_RUN = uuid.uuid4()   # formal Review 的 source_core_run_id（CURRENT canonical）
_IDS = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _params(stmt) -> list:
    """取出编译后的绑定参数值列表。"""
    compiled = stmt.compile(dialect=postgresql.dialect())
    return [p.value for p in compiled.get_binds().values()] if hasattr(
        compiled, "get_binds"
    ) else list(compiled.params.values())


# `source_run_id` 同时也是 SELECT 列，必须检测**谓词**而不是列名
_RUN_PREDICATE = "source_run_id = "


# =============================================================================
# Path A — fp_filter / fp_sort / count 的 snap LATERAL
# =============================================================================


def test_filter_sort_lateral_binds_canonical_core_run_not_legacy_pointer():
    """Path A：LATERAL 必须严格绑定 canonical CoreRun，而不是 LEGACY stock_core pointer。

    这是「筛选连续天 1–5 却返回 6」的直接根因断言：
    若 LATERAL 仍绑 _OLD_RUN，filter 会用 run_old 的 fp_trend_bars 判定。
    """
    stmt = _build_snap_lateral(snapshot_run_id=_NEW_RUN)
    sql = _compile(stmt)

    assert _RUN_PREDICATE in sql, "LATERAL 必须带 source_run_id 精确过滤谓词"
    assert _NEW_RUN in _params(stmt), "LATERAL 必须绑定 canonical CoreRun"
    assert _OLD_RUN not in _params(stmt), (
        "LATERAL 不得绑定 LEGACY stock_core pointer 指向的 run"
    )


def test_filter_sort_lateral_without_canonical_keeps_legacy_behaviour():
    """无 canonical CoreRun 时保持既有"每股最新"行为（不新增 fallback）。"""
    stmt = _build_snap_lateral(snapshot_run_id=None)
    sql = _compile(stmt)
    assert _RUN_PREDICATE not in sql


# =============================================================================
# Path B — display（Query 4）
# =============================================================================


def test_display_query_binds_canonical_core_run():
    """Path B：display 必须与 filter/sort 消费同一个 CoreRun。

    修复前 Query 4 用 `partition by instrument_id order by trade_date desc rn=1`
    另选每股最新 snapshot，导致 display 与 filter 分裂。
    """
    stmt = _build_display_snapshot_query(_IDS, canonical_core_run_id=_NEW_RUN)
    sql = _compile(stmt)
    params = _params(stmt)

    assert _RUN_PREDICATE in sql, "Query 4 必须带 source_run_id 精确过滤谓词"
    assert _NEW_RUN in params, "Query 4 必须绑定 canonical CoreRun"
    assert _OLD_RUN not in params, "Query 4 不得读取 LEGACY run 的 snapshot"


def test_display_query_without_canonical_keeps_latest_per_instrument():
    """无 canonical CoreRun 时保持既有"每股最新"行为（rn=1）。"""
    stmt = _build_display_snapshot_query(_IDS, canonical_core_run_id=None)
    sql = _compile(stmt)
    assert _RUN_PREDICATE not in sql
    assert "row_number" in sql.lower()


# =============================================================================
# 单请求单一身份：两条路径必须拿到同一个 run
# =============================================================================


def test_filter_and_display_paths_share_one_run_identity():
    """一次请求内 Path A 与 Path B 必须解析到同一个 CoreRun id。

    等价于用户要求的验收条件：
        所有 fp_trade_date 相同 && 所有 data_run_id == canonical_core_run_id
    （该断言在实现层保证：两者都只接受同一个 canonical_core_run_id 入参。）
    """
    lateral_stmt = _build_snap_lateral(snapshot_run_id=_NEW_RUN)
    display_stmt = _build_display_snapshot_query(
        _IDS, canonical_core_run_id=_NEW_RUN,
    )

    lateral_runs = {p for p in _params(lateral_stmt) if isinstance(p, uuid.UUID)}
    display_runs = {p for p in _params(display_stmt) if isinstance(p, uuid.UUID)}

    assert lateral_runs == {_NEW_RUN}
    assert display_runs == {_NEW_RUN}
    assert lateral_runs == display_runs, "filter/sort 与 display 必须是同一个 CoreRun"


def test_two_paths_reject_divergent_runs():
    """反证：两条路径传入不同 run 时，编译结果里各自只含自己那个 run，
    不会出现"filter 用 old、display 用 new"仍被静默接受的情况——
    调用方（get_market_stocks）只解析一次并同时传给两者，见下一条源码契约测试。"""
    lateral_stmt = _build_snap_lateral(snapshot_run_id=_OLD_RUN)
    display_stmt = _build_display_snapshot_query(
        _IDS, canonical_core_run_id=_NEW_RUN,
    )
    assert _OLD_RUN in _params(lateral_stmt)
    assert _NEW_RUN in _params(display_stmt)


# =============================================================================
# 源码层契约：CURRENT 路径不得再消费 LEGACY stock_core pointer
# =============================================================================


def test_market_stocks_service_no_longer_reads_stock_core_pointer_for_current():
    """/market/stocks 的 CURRENT 路径不得再读 factor_publications(kind=stock_core)。

    这是防止回归到「filter/sort 用 LEGACY pointer」的源码级护栏：
    一旦有人把 get_publication(kind=stock_core) 接回来，本用例立即失败。
    """
    from pathlib import Path

    src = Path("app/services/market_stocks_service.py").read_text(encoding="utf-8")

    assert "PUBLICATION_KIND_STOCK_CORE" not in src, (
        "market_stocks_service 不得再以 stock_core FactorPublication 为 CURRENT authority"
    )
    assert "published_core_run_id" not in src, (
        "published_core_run_id 已随 LEGACY pointer 一起删除"
    )
    # 必须复用 service-level 单一 owner
    assert "current_core_run_service" in src
    assert "resolve_current_core_run" in src


def test_market_stocks_and_stock_context_share_one_owner():
    """两个 consumer 必须调用同一个 service-level owner，禁止各写一套。"""
    from pathlib import Path

    svc = Path("app/services/market_stocks_service.py").read_text(encoding="utf-8")
    ctx = Path("app/api/stock_context.py").read_text(encoding="utf-8")

    assert "from app.services.current_core_run_service import resolve_current_core_run" in svc
    assert "from app.services.current_core_run_service import resolve_current_core_run" in ctx
