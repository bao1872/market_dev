"""/first-pyramid CURRENT Core run resolution 回归测试（纯单元，无数据库）。

背景（AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 consumer migration）：

- ``2ea7a3d``（2026-07-29）把 ``stock_context._find_latest_succeeded_run`` /
  ``_find_run_by_trade_date`` 改为**优先读 stock_core FactorPublication**。
- ``60c5d267``（2026-08-27 05:28 +0800）把 CURRENT AfterClose 主链切成
  Core → Review，**不再推进 stock_core pointer**（仅 LEGACY compatibility）。
- 该架构迁移**没有修改** ``backend/app/api/stock_context.py``（最后改动 2026-08-01），
  于是 ``/first-pyramid`` 被永久 pin 在最后一次旧架构发布日 2026-08-26 / run ``ca5c3dd2``。

本文件只覆盖 ``_resolve_current_core_run``（CURRENT 第一金字塔的唯一 Core 解析入口）：

1. 本次真实回归：存在 stale stock_core pointer 时仍须解析到 formal Review 的 Core
2. stale legacy pointer 不得 pin CURRENT
3. as_of 必须 point-in-time，不得返回更晚的 Review/Core
4. lineage fail-closed：Core 缺失 / status != succeeded / trade_date 不一致 → None

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_first_pyramid_current_core_resolution.py -v
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.stock_context import _resolve_current_core_run
from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
from app.services.feature_snapshot_service import _SCHEMA_VERSION

# 生产事故当天的真实日期/run 语义（仅作可读常量，不连库）
_STALE_CORE_DATE = date(2026, 8, 26)
_CURRENT_CORE_DATE = date(2026, 9, 1)


class _RowsResult:
    """list_formally_published_review_dates 消费：``for row in result``。"""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _ScalarResult:
    """_get_publication 消费：``result.scalar_one_or_none()``。"""

    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _review_run(
    *,
    run_id: uuid.UUID,
    trade_date: date,
    core_run_id: uuid.UUID | None,
    status: str = "published",
    published_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        trade_date=trade_date,
        source_core_run_id=core_run_id,
        status=status,
        published_at=published_at
        if published_at is not None
        else datetime(2026, 9, 1, 17, 47, 27),
    )


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
    execute_results: list[object],
    objects_by_id: dict[uuid.UUID, object],
) -> AsyncMock:
    """构造最小 AsyncSession 替身。

    ``execute_results`` 按调用顺序返回；**超出预期次数的 execute 立即失败**——
    这正是"stale stock_core pointer 未被读取"的证明手段：任何额外的
    pointer 查询（例如查 stock_core）都会让断言炸掉。
    """
    session = AsyncMock()
    pending = iter(execute_results)

    async def _execute(stmt, *args, **kwargs):
        try:
            return next(pending)
        except StopIteration:
            raise AssertionError(
                "被测函数产生了超出预期的 execute 调用（可能读取了 stock_core pointer）"
            ) from None

    async def _get(model, ident, *args, **kwargs):
        return objects_by_id.get(ident)

    session.execute = AsyncMock(side_effect=_execute)
    session.get = AsyncMock(side_effect=_get)
    return session


def _session_with_pointer(
    *,
    formal_dates: list[date],
    review_run: SimpleNamespace,
    objects_by_id: dict[uuid.UUID, object],
) -> AsyncMock:
    """execute：第 1 次返回正式 Review 日期，第 2 次返回 live pointer（data_run_id）。"""
    pointer = SimpleNamespace(data_run_id=review_run.id)
    return _build_session(
        [
            _RowsResult([(d,) for d in formal_dates]),
            _ScalarResult(pointer),
        ],
        objects_by_id,
    )


# =============================================================================
# Case 1 / Case 2
# =============================================================================


async def test_current_core_follows_formal_review_not_stale_stock_core_pointer():
    """Case 1（本次真实回归）+ Case 2（stale legacy pointer 不得 pin CURRENT）。

    生产等价状态：
      - stale ``stock_core`` pointer → Core(2026-08-26)
      - formal ``market_review``      → Review(2026-09-01)
      - Review.source_core_run_id     → Core(2026-09-01)（status=succeeded）

    断言：解析结果必须是 Core(2026-09-01)，不是 2026-08-26。
    本用例不注入任何 stock_core pointer 读取路径——被测函数根本不查它，
    因此 stale pointer 无论是否有效都不可能影响 CURRENT 结果。
    """
    stale_core_id = uuid.uuid4()
    current_core_id = uuid.uuid4()
    review_id = uuid.uuid4()

    review = _review_run(
        run_id=review_id,
        trade_date=_CURRENT_CORE_DATE,
        core_run_id=current_core_id,
    )
    current_core = _core_run(run_id=current_core_id, trade_date=_CURRENT_CORE_DATE)
    stale_core = _core_run(run_id=stale_core_id, trade_date=_STALE_CORE_DATE)

    # stale Core 也在 objects_by_id 里：反证它即使"可查到"也不会被选中
    session = _session_with_pointer(
        formal_dates=[_CURRENT_CORE_DATE],
        review_run=review,
        objects_by_id={
            review_id: review,
            current_core_id: current_core,
            stale_core_id: stale_core,
        },
    )

    resolved = await _resolve_current_core_run(session)

    assert resolved is not None, "formal Review 血统完整时必须解析出 CoreRun"
    assert resolved.id == current_core_id
    assert resolved.trade_date == _CURRENT_CORE_DATE
    # 显式反证：不得返回 stale Core
    assert resolved.id != stale_core_id
    assert resolved.trade_date != _STALE_CORE_DATE


async def test_stale_legacypointer_never_consulted():
    """Case 2 强化：即使 DB 里存在有效的 stale stock_core pointer，
    CURRENT 解析也不得读它——被测函数的 execute 调用次数被严格限定为 formal Review 链路。
    """
    current_core_id = uuid.uuid4()
    review_id = uuid.uuid4()

    review = _review_run(
        run_id=review_id,
        trade_date=_CURRENT_CORE_DATE,
        core_run_id=current_core_id,
    )
    current_core = _core_run(run_id=current_core_id, trade_date=_CURRENT_CORE_DATE)

    session = _session_with_pointer(
        formal_dates=[_CURRENT_CORE_DATE],
        review_run=review,
        objects_by_id={review_id: review, current_core_id: current_core},
    )

    await _resolve_current_core_run(session)

    # 只允许 2 次 execute：正式 Review 日期 + live pointer。
    # 任何第 3 次（例如查 stock_core pointer）都会在此被拦下。
    assert session.execute.await_count == 2


# =============================================================================
# Case 3
# =============================================================================


async def test_as_of_is_point_in_time_and_never_returns_later_core():
    """Case 3：formal Review 覆盖 08-28 / 08-31 / 09-01 时，
    ``as_of=2026-08-31`` 必须解析到 08-31 Review 的 Core，禁止返回 09-01。
    """
    core_0901 = uuid.uuid4()
    core_0831 = uuid.uuid4()
    core_0828 = uuid.uuid4()
    review_0831 = uuid.uuid4()

    review = _review_run(
        run_id=review_0831,
        trade_date=date(2026, 8, 31),
        core_run_id=core_0831,
        published_at=datetime(2026, 8, 31, 17, 44, 24),
    )
    objects = {
        review_0831: review,
        core_0831: _core_run(run_id=core_0831, trade_date=date(2026, 8, 31)),
        core_0901: _core_run(run_id=core_0901, trade_date=date(2026, 9, 1)),
        core_0828: _core_run(run_id=core_0828, trade_date=date(2026, 8, 28)),
    }

    session = _session_with_pointer(
        formal_dates=[date(2026, 9, 1), date(2026, 8, 31), date(2026, 8, 28)],
        review_run=review,
        objects_by_id=objects,
    )

    resolved = await _resolve_current_core_run(session, as_of=date(2026, 8, 31))

    assert resolved is not None
    assert resolved.id == core_0831
    assert resolved.trade_date == date(2026, 8, 31)
    assert resolved.id != core_0901, "as_of 不得返回晚于截止日的 Core（禁止未来数据）"


# =============================================================================
# Case 4
# =============================================================================


@pytest.mark.parametrize(
    ("case", "core_status", "core_trade_date", "core_exists", "core_id_is_none"),
    [
        ("core_run_missing", STATUS_SUCCEEDED, _CURRENT_CORE_DATE, False, False),
        ("core_not_succeeded", "failed", _CURRENT_CORE_DATE, True, False),
        ("core_cross_date", STATUS_SUCCEEDED, date(2026, 8, 26), True, False),
        ("review_without_source_core", STATUS_SUCCEEDED, _CURRENT_CORE_DATE, True, True),
    ],
)
async def test_lineage_fail_closed_never_falls_back_to_arbitrary_core(
    case: str,
    core_status: str,
    core_trade_date: date,
    core_exists: bool,
    core_id_is_none: bool,
):
    """Case 4：Review.source_core_run_id 不存在 / Core status != succeeded /
    Core.trade_date != Review.trade_date —— 一律 fail-closed 返回 None，
    绝不偷偷 fallback 到 arbitrary latest succeeded Core。
    """
    other_core_id = uuid.uuid4()  # 一个"任意最新 succeeded Core"，用于反证不得被选中
    core_id = uuid.uuid4()
    review_id = uuid.uuid4()

    review = _review_run(
        run_id=review_id,
        trade_date=_CURRENT_CORE_DATE,
        core_run_id=None if core_id_is_none else core_id,
    )

    objects: dict[uuid.UUID, object] = {review_id: review}
    if core_exists and not core_id_is_none:
        objects[core_id] = _core_run(
            run_id=core_id, trade_date=core_trade_date, status=core_status,
        )
    objects[other_core_id] = _core_run(
        run_id=other_core_id, trade_date=_CURRENT_CORE_DATE, status=STATUS_SUCCEEDED,
    )

    session = _session_with_pointer(
        formal_dates=[_CURRENT_CORE_DATE],
        review_run=review,
        objects_by_id=objects,
    )

    resolved = await _resolve_current_core_run(session)

    assert resolved is None, f"{case}: 血统不完整时必须 fail-closed"


async def test_no_formal_review_returns_none():
    """无正式发布 Review（含 as_of 早于所有正式日期）→ None，不得回退。"""
    session = _build_session([_RowsResult([])], {})
    assert await _resolve_current_core_run(session) is None

    session2 = _build_session([_RowsResult([(date(2026, 9, 1),)])], {})
    # as_of 早于全部正式日期 → 候选集合为空，不应发生第 2 次 execute
    assert await _resolve_current_core_run(session2, as_of=date(2026, 8, 1)) is None
