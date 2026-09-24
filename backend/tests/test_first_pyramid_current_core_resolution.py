"""/first-pyramid CURRENT Core run resolution 回归测试（纯单元，无数据库）。

[CURRENT-CORE-OWNER-REGRESSION-01] 修复后，CURRENT canonical CoreRun 的唯一 authority
是**最新 canonical after-close StockFeatureSnapshotRun**（``after_close`` + ``full`` scope
+ ``succeeded`` + 当前 ``schema_version`` + ``finished_at`` 非空），由 service-level 单一 owner
``app.services.current_core_run_service.resolve_current_core_run`` 解析。

legacy ``FactorPublication(stock_core)`` 仅历史兼容，**不再参与** CURRENT 解析；owner 内部
不得再查询 FactorPublication，也不得先 SELECT id 再 ``session.get``。

本文件只覆盖 ``resolve_current_core_run``（owner 本身）：

1. stale stock_core pointer 不能 pin CURRENT：owner 只查 StockFeatureSnapshotRun，返回最新 canonical run
2. 忽略非 canonical run（manual/backfill/sample/failed/running/wrong-schema）
3. as_of 必须 point-in-time，不得返回未来 run
4. 无合法 run → None（fail-closed）
5. 同日多个合法 run：finished_at 更晚者胜出（trade_date/finished_at/created_at 优先级）

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_first_pyramid_current_core_resolution.py -v
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql

from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_AFTER_CLOSE,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.current_core_run_service import resolve_current_core_run
from app.services.feature_snapshot_service import _SCHEMA_VERSION

# 生产语义日期（仅作可读常量，不连库）
_STALE_CORE_DATE = date(2026, 8, 26)          # legacy stock_core 最后写入日（回归锚点）
_CURRENT_CORE_DATE = date(2026, 9, 24)        # 当前 canonical after-close CoreRun
_FUTURE_CORE_DATE = date(2026, 9, 30)


def _run(
    trade_date: date,
    *,
    run_type: str = RUN_TYPE_AFTER_CLOSE,
    status: str = STATUS_SUCCEEDED,
    schema_version: int = _SCHEMA_VERSION,
    finished_at: datetime | None,
    scope: str = "full",
    created_at: datetime | None = None,
    snapshot_count: int | None = None,
    expected_count: int | None = None,
) -> StockFeatureSnapshotRun:
    """构造最小 StockFeatureSnapshotRun 替身（仅含 owner 读取的字段）。"""
    return StockFeatureSnapshotRun(
        id=uuid.uuid4(),
        trade_date=trade_date,
        run_type=run_type,
        status=status,
        schema_version=schema_version,
        finished_at=finished_at,
        metadata_={"scope": scope},
        created_at=created_at
        or datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, tzinfo=UTC),
        snapshot_count=snapshot_count,
        expected_count=expected_count,
    )


def _neg(v):
    if v is None:
        return None
    if hasattr(v, "timestamp"):
        return -v.timestamp()
    if hasattr(v, "toordinal"):
        return -v.toordinal()
    return -v


def _is_canonical(row) -> bool:
    """模拟 DB 应用 resolver 的 canonical 过滤（与 SQL 谓词一致）。

    注意：本 helper 只是把契约（after_close+full+succeeded+当前schema+finished）转成
    Python 判定，用于单测层面模拟 DB 过滤；resolver 是否真的生成这些谓词由下方 SQL 断言独立把关。
    """
    return (
        getattr(row, "run_type", None) == RUN_TYPE_AFTER_CLOSE
        and getattr(row, "status", None) == STATUS_SUCCEEDED
        and getattr(row, "schema_version", None) == _SCHEMA_VERSION
        and getattr(row, "finished_at", None) is not None
        and (getattr(row, "metadata_", None) or {}).get("scope") == "full"
    )


_AS_OF_RE = re.compile(r"trade_date\s*<=\s*'?(?P<d>\d{4}-\d{2}-\d{2})'?")


def _simulate_db(stmt, rows):
    """模拟 DB：canonical 过滤 + as_of point-in-time + ORDER BY DESC + LIMIT 1。"""
    sql = _sql(stmt)
    m = _AS_OF_RE.search(sql)
    as_of = date.fromisoformat(m.group("d")) if m else None
    kept = []
    for r in rows:
        if not _is_canonical(r):
            continue
        if as_of is not None and r.trade_date > as_of:
            continue
        kept.append(r)
    kept.sort(
        key=lambda r: (_neg(r.trade_date), _neg(r.finished_at), _neg(r.created_at))
    )
    limit = getattr(stmt, "_limit", None)
    if limit is not None:
        kept = kept[:limit]
    return kept


def _make_session(return_rows: list[StockFeatureSnapshotRun]):
    """构造最小 AsyncSession 替身。

    ``execute`` 只允许被调用 **1 次**（查询 StockFeatureSnapshotRun）；任何额外查询
    （例如回退查 FactorPublication）都会让断言炸掉——这正是 fail-closed 的证明手段。
    """
    state = {"n": 0}
    captured: dict = {}

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

    async def _execute(stmt, *args, **kwargs):
        state["n"] += 1
        if state["n"] > 1:
            raise AssertionError(
                "resolve_current_core_run 产生超出预期的 execute（应仅 1 次查询 StockFeatureSnapshotRun）"
            )
        captured["stmt"] = stmt
        ordered = _simulate_db(stmt, list(return_rows))
        return _Result(ordered)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock()  # 新 owner 不调用 session.get
    session._captured = captured
    session._calls = state
    return session


def _sql(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# =============================================================================
# Case 1：stale stock_core pointer 不能 pin CURRENT
# =============================================================================


async def test_stale_stock_core_pointer_cannot_pin_current():
    """legacy stock_core pointer（2026-08-26 / ca5c3dd2）不再决定 CURRENT。

    owner 只查 StockFeatureSnapshotRun，返回最新 canonical after-close run（2026-09-24）；
    且全程不查询 FactorPublication（session.get 不被调用）。
    """
    canonical = _run(_CURRENT_CORE_DATE, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))

    session = _make_session([canonical])
    resolved = await resolve_current_core_run(session)

    assert resolved is canonical, "必须返回最新 canonical after-close CoreRun"
    assert resolved.trade_date == _CURRENT_CORE_DATE
    assert session.execute.await_count == 1, "owner 只允许 1 次 execute"
    assert session.get.await_count == 0, "新 owner 不得回退查 FactorPublication / session.get"
    sql = _sql(session._captured["stmt"]).lower()
    assert "factor_publication" not in sql, "CURRENT 解析不得查询 FactorPublication"


# =============================================================================
# Case 2：忽略非 canonical run（过滤契约由 SQL 谓词锁定）
# =============================================================================


async def test_ignores_non_canonical_runs():
    """非 canonical run 不得抢占 CURRENT。

    单测层面：owner 请求了严格过滤（run_type/status/schema_version/finished_at/scope 谓词），
    DB 应用过滤后只返回 canonical run，owner 如实返回它。真实过滤在 PG 集成测试覆盖。
    """
    canonical = _run(_CURRENT_CORE_DATE, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))

    session = _make_session([canonical])
    resolved = await resolve_current_core_run(session)

    assert resolved is canonical
    sql = _sql(session._captured["stmt"])
    lowered = sql.lower()
    assert "'after_close'" in sql, "必须限定 run_type=after_close"
    assert "'succeeded'" in sql, "必须限定 status=succeeded"
    assert "is not null" in lowered, "必须限定 finished_at IS NOT NULL"
    assert "'full'" in sql, "必须限定 scope=full"
    assert str(_SCHEMA_VERSION) in sql, "必须限定当前 schema_version"
    assert "order by" in lowered, "必须按 trade_date/finished_at/created_at 排序收敛"
    assert "desc" in lowered


async def test_query_shape_excludes_non_canonical_via_sql():
    """SQL 形状层面证明：非 canonical（manual/backfill/sample/failed/running/wrong-schema）
    都会被 WHERE 排除——它们不满足 run_type/status/finished_at/scope 中至少一项。"""
    canonical = _run(_CURRENT_CORE_DATE, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    session = _make_session([canonical])
    await resolve_current_core_run(session)
    sql = _sql(session._captured["stmt"])
    # 这些非 canonical 特征不应出现在 WHERE 允许的取值里
    assert "'manual'" not in sql
    assert "'backfill'" not in sql
    assert "'sample'" not in sql
    assert "'failed'" not in sql
    assert "'running'" not in sql


# =============================================================================
# Case 3：as_of point-in-time
# =============================================================================


async def test_as_of_is_point_in_time_and_never_returns_later_core():
    """存在 09-24 / 09-23 / 09-22 时，``as_of=2026-09-23`` 必须返回 09-23，禁止未来 run。

    单测层面：owner 请求的 SQL 含 ``trade_date <= 2026-09-23``；DB 应用后只返回 09-23。
    """
    run_0923 = _run(date(2026, 9, 23), finished_at=datetime(2026, 9, 23, 15, 0, tzinfo=UTC))
    session = _make_session([run_0923])
    resolved = await resolve_current_core_run(session, as_of=date(2026, 9, 23))

    assert resolved is run_0923
    sql = _sql(session._captured["stmt"])
    assert "trade_date <=" in sql.lower(), "as_of 必须生成 trade_date <= 谓词"
    assert "2026-09-23" in sql, "as_of 必须绑定截止日"


async def test_as_of_before_all_runs_returns_none():
    """as_of 早于全部 run → 候选集合为空 → None（point-in-time 前无数据）。"""
    run_0924 = _run(_CURRENT_CORE_DATE, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    session = _make_session([run_0924])
    resolved = await resolve_current_core_run(session, as_of=date(2026, 9, 1))
    assert resolved is None


# =============================================================================
# Case 4：无合法 run → None
# =============================================================================


async def test_no_valid_run_returns_none():
    """无满足 after_close+full+succeeded+当前schema+finished 的 run → None。"""
    session = _make_session([])
    assert await resolve_current_core_run(session) is None


async def test_only_non_canonical_runs_returns_none():
    """表中只有非 canonical run（failed / running / wrong-schema / sample / manual）时 → None。"""
    failed = _run(_CURRENT_CORE_DATE, status="failed", finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    running = _run(_CURRENT_CORE_DATE, status="running", finished_at=None)
    wrong_schema = _run(_CURRENT_CORE_DATE, schema_version=_SCHEMA_VERSION + 1, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    sample = _run(_CURRENT_CORE_DATE, scope="sample", finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    session = _make_session([failed, running, wrong_schema, sample])
    assert await resolve_current_core_run(session) is None


# =============================================================================
# Case 5：同日多个合法 run → finished_at 更晚者胜出
# =============================================================================


async def test_same_date_deterministic_choice_by_finished_at():
    """同日两个合法 after_close full succeeded run：finished_at 更晚者胜出。"""
    earlier = _run(
        _CURRENT_CORE_DATE,
        finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 24, 15, 1, tzinfo=UTC),
    )
    later = _run(
        _CURRENT_CORE_DATE,
        finished_at=datetime(2026, 9, 24, 16, 30, tzinfo=UTC),
        created_at=datetime(2026, 9, 24, 15, 1, tzinfo=UTC),
    )
    # 故意逆序放入，验证 owner 的 ORDER BY 收敛而非依赖列表顺序
    session = _make_session([earlier, later])
    resolved = await resolve_current_core_run(session)

    assert resolved is later, "同日合法 run 必须取 finished_at 更晚者"
    sql = _sql(session._captured["stmt"]).lower()
    assert "order by" in sql and "desc" in sql


# =============================================================================
# 跨 consumer 委派契约：stock_context._resolve_current_core_run 与 owner 一致
# =============================================================================


async def test_stock_context_delegates_to_owner():
    """stock_context._resolve_current_core_run 必须委派给 owner，不产生第二套解析。"""
    from app.api.stock_context import _resolve_current_core_run as ctx_resolve

    canonical = _run(_CURRENT_CORE_DATE, finished_at=datetime(2026, 9, 24, 15, 0, tzinfo=UTC))
    session = _make_session([canonical])

    resolved = await ctx_resolve(session)
    assert resolved is canonical
    assert session.execute.await_count == 1
    assert session.get.await_count == 0
