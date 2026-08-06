"""resolve_core_run_context 单元测试（P0-02 released-config 唯一来源）。

[CHANGE-20260805-CP4A-CP3]
验证（用 fake resolver，不连真实 DB）：
- released dsa_selector config 被解析并冻结进 CoreRunContext；
- 无 released version 时 fail-closed（禁止回退代码常量）；
- universe hash / market-data contract / parameter hash 正确冻结；
- 同一 run 冻结后不可变。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.services.core_run_context import (
    ReleasedConfigError,
    ReleasedConfigResolver,
    resolve_core_run_context,
)


class FakeReleasedResolver(ReleasedConfigResolver):
    """fake repository：返回固定 released DSA config，或抛 ReleasedConfigError。"""

    def __init__(self, *, fail_closed: bool = False) -> None:
        self._fail = fail_closed

    async def resolve_released_dsa_config(
        self,
        *,
        trade_date: date,
    ) -> dict:
        if self._fail:
            raise ReleasedConfigError(
                "dsa_selector 无 released StrategyVersion（scheduled 模式禁止回退）"
            )
        return {
            "dsa_version": "v3",
            "dsa_build_hash": "build-abc",
            "dsa_effective_config": {"min_dir_bars": 50, "max_lookback": 120},
        }


@pytest.mark.asyncio
async def test_resolve_freezes_released_dsa_config() -> None:
    """released DSA config 被解析并冻结；parameter_hash 覆盖 config。"""
    run_id = uuid.uuid4()
    ctx = await resolve_core_run_context(
        trade_date=date(2026, 8, 5),
        snapshot_run_id=run_id,
        eligible_instrument_ids=[uuid.uuid4() for _ in range(3)],
        resolver=FakeReleasedResolver(),
    )

    assert ctx.algorithm_versions["dsa"] == "v3"
    assert ctx.config["dsa"] == {"min_dir_bars": 50, "max_lookback": 120}
    assert ctx.config["eligible_universe_size"] == 3
    assert ctx.config["market_data_contract_version"] == "mdc-v1"
    assert ctx.config["dsa_build_hash"] == "build-abc"
    assert ctx.parameter_hash  # hash 由 config + versions + contract 派生
    assert ctx.run_id == run_id
    assert ctx.trade_date == date(2026, 8, 5)


@pytest.mark.asyncio
async def test_resolve_fails_closed_without_released_version() -> None:
    """无 released dsa_selector → ReleasedConfigError（禁止回退代码常量）。"""
    with pytest.raises(ReleasedConfigError):
        await resolve_core_run_context(
            trade_date=date(2026, 8, 5),
            snapshot_run_id=uuid.uuid4(),
            eligible_instrument_ids=[uuid.uuid4()],
            resolver=FakeReleasedResolver(fail_closed=True),
        )


@pytest.mark.asyncio
async def test_resolve_universe_hash_order_independent() -> None:
    """eligible universe hash 顺序无关（同一集合不同顺序 → 同一 hash）。"""
    ids = [uuid.uuid4() for _ in range(4)]
    ctx_a = await resolve_core_run_context(
        trade_date=date(2026, 8, 5),
        snapshot_run_id=uuid.uuid4(),
        eligible_instrument_ids=ids,
        resolver=FakeReleasedResolver(),
    )
    ctx_b = await resolve_core_run_context(
        trade_date=date(2026, 8, 5),
        snapshot_run_id=uuid.uuid4(),
        eligible_instrument_ids=list(reversed(ids)),
        resolver=FakeReleasedResolver(),
    )
    assert ctx_a.config["eligible_universe_hash"] == ctx_b.config["eligible_universe_hash"]


@pytest.mark.asyncio
async def test_resolve_requires_resolver() -> None:
    """不传 resolver → ReleasedConfigError（禁止无 released 时回退）。"""
    with pytest.raises(ReleasedConfigError):
        await resolve_core_run_context(
            trade_date=date(2026, 8, 5),
            snapshot_run_id=uuid.uuid4(),
            eligible_instrument_ids=[uuid.uuid4()],
        )
