"""/first-pyramid CURRENT Core run resolution 回归测试（纯单元，无数据库）。

[REVIEW-V2-R1 runtime closure] 旧 Review 产品已退役，CURRENT canonical CoreRun 的
唯一 authority 恢复为 **live stock_core FactorPublication pointer**
（``publication_kind=stock_core``、``scope_type/scope_key='market'``、
``superseded_by IS NULL``）。``stock_context._resolve_current_core_run`` 只是委派给
service-level 单一 owner ``app.services.current_core_run_service.resolve_current_core_run``
（禁止在本模块复制第二套解析）。

本文件只覆盖 ``_resolve_current_core_run``：

1. live stock_core pointer → data_run_id → StockFeatureSnapshotRun（succeeded）解析成功
2. 多个 live pointer 时取 point-in-time 下最新者，旧（stale）pointer 不得抢占
3. as_of 必须 point-in-time，不得返回晚于截止日的 Core
4. lineage fail-closed：无 live pointer / data_run_id 为空 / Core 缺失 /
   status != succeeded / trade_date 不一致 / schema_version 不一致 → None
   （**禁止**回退到 arbitrary latest succeeded CoreRun）

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_first_pyramid_current_core_resolution.py -v
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.stock_context import _resolve_current_core_run
from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
from app.services.feature_snapshot_service import _SCHEMA_VERSION

# 生产语义日期（仅作可读常量，不连库）
_STALE_CORE_DATE = date(2026, 8, 26)
_CURRENT_CORE_DATE = date(2026, 9, 1)


class _ScalarsRows:
    """``session.execute(...)`` 结果替身：``.scalars().all()`` 返回 live pointer 列表。"""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _pointer(*, trade_date: date, data_run_id: uuid.UUID | None) -> SimpleNamespace:
    """live stock_core FactorPublication pointer 的最小替身。

    owner 只消费 ``.trade_date``（point-in-time 过滤 / 一致性校验）与
    ``.data_run_id``（canonical CoreRun 外键）。
    """
    return SimpleNamespace(trade_date=trade_date, data_run_id=data_run_id)


def _core_run(
    *,
    run_id: uuid.UUID,
    trade_date: date,
    status: str = STATUS_SUCCEEDED,
    schema_version: int = _SCHEMA_VERSION,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        trade_date=trade_date,
        status=status,
        schema_version=schema_version,
    )


def _build_session(
    pointer_rows: list[SimpleNamespace],
    objects_by_id: dict[uuid.UUID, object],
) -> AsyncMock:
    """构造最小 AsyncSession 替身。

    ``execute`` 只允许被调用 **1 次**（读取 live stock_core pointer）；任何额外查询
    （例如回退查 arbitrary succeeded Core）都会让断言炸掉——这正是 fail-closed 的证明手段。
    ``get`` 按 id 返回 CoreRun（``StockFeatureSnapshotRun``）。
    """
    session = AsyncMock()
    pending = iter([_ScalarsRows(pointer_rows)])

    async def _execute(stmt, *args, **kwargs):
        try:
            return next(pending)
        except StopIteration:
            raise AssertionError(
                "被测函数产生了超出预期的 execute 调用（只应读取一次 live stock_core pointer）"
            ) from None

    async def _get(model, ident, *args, **kwargs):
        return objects_by_id.get(ident)

    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock(side_effect=_get)
    return session


# =============================================================================
# Case 1 / Case 2：live pointer 解析 + stale pointer 不得抢占
# =============================================================================


async def test_current_core_resolves_from_live_stock_core_pointer():
    """live stock_core pointer（最新）→ data_run_id → succeeded CoreRun 解析成功。

    pointer 行内含一个更旧的 stale pointer；owner 取 trade_date 最新者，
    旧 pointer 不得抢占（且 stale Core 即使可查到也不会被选中）。
    """
    stale_core_id = uuid.uuid4()
    current_core_id = uuid.uuid4()

    session = _build_session(
        pointer_rows=[
            _pointer(trade_date=_CURRENT_CORE_DATE, data_run_id=current_core_id),
            _pointer(trade_date=_STALE_CORE_DATE, data_run_id=stale_core_id),
        ],
        objects_by_id={
            current_core_id: _core_run(run_id=current_core_id, trade_date=_CURRENT_CORE_DATE),
            stale_core_id: _core_run(run_id=stale_core_id, trade_date=_STALE_CORE_DATE),
        },
    )

    resolved = await _resolve_current_core_run(session)

    assert resolved is not None, "live stock_core pointer 血统完整时必须解析出 CoreRun"
    assert resolved.id == current_core_id
    assert resolved.trade_date == _CURRENT_CORE_DATE
    # 显式反证：不得返回 stale Core
    assert resolved.id != stale_core_id
    assert resolved.trade_date != _STALE_CORE_DATE


async def test_single_execute_only_reads_live_pointer():
    """owner 只允许 1 次 execute（读取 live stock_core pointer），
    不得为回退到 arbitrary Core 追加查询。"""
    core_id = uuid.uuid4()
    session = _build_session(
        pointer_rows=[_pointer(trade_date=_CURRENT_CORE_DATE, data_run_id=core_id)],
        objects_by_id={core_id: _core_run(run_id=core_id, trade_date=_CURRENT_CORE_DATE)},
    )

    await _resolve_current_core_run(session)

    assert session.execute.await_count == 1


# =============================================================================
# Case 3：as_of point-in-time
# =============================================================================


async def test_as_of_is_point_in_time_and_never_returns_later_core():
    """live pointer 覆盖 09-01 / 08-31 / 08-28 时，``as_of=2026-08-31``
    必须解析到 08-31 的 Core，禁止返回 09-01。"""
    core_0901 = uuid.uuid4()
    core_0831 = uuid.uuid4()
    core_0828 = uuid.uuid4()

    session = _build_session(
        pointer_rows=[
            _pointer(trade_date=date(2026, 9, 1), data_run_id=core_0901),
            _pointer(trade_date=date(2026, 8, 31), data_run_id=core_0831),
            _pointer(trade_date=date(2026, 8, 28), data_run_id=core_0828),
        ],
        objects_by_id={
            core_0901: _core_run(run_id=core_0901, trade_date=date(2026, 9, 1)),
            core_0831: _core_run(run_id=core_0831, trade_date=date(2026, 8, 31)),
            core_0828: _core_run(run_id=core_0828, trade_date=date(2026, 8, 28)),
        },
    )

    resolved = await _resolve_current_core_run(session, as_of=date(2026, 8, 31))

    assert resolved is not None
    assert resolved.id == core_0831
    assert resolved.trade_date == date(2026, 8, 31)
    assert resolved.id != core_0901, "as_of 不得返回晚于截止日的 Core（禁止未来数据）"


# =============================================================================
# Case 4：lineage fail-closed
# =============================================================================


@pytest.mark.parametrize(
    (
        "case",
        "core_status",
        "core_trade_date",
        "core_exists",
        "data_run_id_none",
        "schema_version",
    ),
    [
        ("core_run_missing", STATUS_SUCCEEDED, _CURRENT_CORE_DATE, False, False, _SCHEMA_VERSION),
        ("core_not_succeeded", "failed", _CURRENT_CORE_DATE, True, False, _SCHEMA_VERSION),
        ("core_cross_date", STATUS_SUCCEEDED, _STALE_CORE_DATE, True, False, _SCHEMA_VERSION),
        ("schema_version_mismatch", STATUS_SUCCEEDED, _CURRENT_CORE_DATE, True, False, _SCHEMA_VERSION + 1),
        ("pointer_without_data_run_id", STATUS_SUCCEEDED, _CURRENT_CORE_DATE, True, True, _SCHEMA_VERSION),
    ],
)
async def test_lineage_fail_closed_never_falls_back_to_arbitrary_core(
    case: str,
    core_status: str,
    core_trade_date: date,
    core_exists: bool,
    data_run_id_none: bool,
    schema_version: int,
):
    """pointer.data_run_id 为空 / Core 不存在 / status != succeeded /
    Core.trade_date != pointer.trade_date / schema_version 不一致
    —— 一律 fail-closed 返回 None，绝不 fallback 到 arbitrary latest succeeded Core。
    """
    other_core_id = uuid.uuid4()  # 一个"任意最新 succeeded Core"，用于反证不得被选中
    core_id = uuid.uuid4()

    objects: dict[uuid.UUID, object] = {}
    if core_exists and not data_run_id_none:
        objects[core_id] = _core_run(
            run_id=core_id,
            trade_date=core_trade_date,
            status=core_status,
            schema_version=schema_version,
        )
    objects[other_core_id] = _core_run(
        run_id=other_core_id, trade_date=_CURRENT_CORE_DATE, status=STATUS_SUCCEEDED,
    )

    session = _build_session(
        pointer_rows=[
            _pointer(
                trade_date=_CURRENT_CORE_DATE,
                data_run_id=None if data_run_id_none else core_id,
            )
        ],
        objects_by_id=objects,
    )

    resolved = await _resolve_current_core_run(session)

    assert resolved is None, f"{case}: 血统不完整时必须 fail-closed"


async def test_no_live_pointer_returns_none():
    """无 live stock_core pointer → None，不得回退。"""
    session = _build_session(pointer_rows=[], objects_by_id={})
    assert await _resolve_current_core_run(session) is None


async def test_as_of_before_all_pointers_returns_none():
    """as_of 早于全部 live pointer → 候选集合为空 → None（point-in-time 前无数据）。"""
    core_id = uuid.uuid4()
    session = _build_session(
        pointer_rows=[_pointer(trade_date=date(2026, 9, 1), data_run_id=core_id)],
        objects_by_id={core_id: _core_run(run_id=core_id, trade_date=date(2026, 9, 1))},
    )
    assert await _resolve_current_core_run(session, as_of=date(2026, 8, 1)) is None
