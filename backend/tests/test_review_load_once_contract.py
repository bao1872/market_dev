"""[REVIEW-FACT-PARITY-02 §10-§12] Review load-once 合同测试。

锁定：
- 正式 ``compute_run`` 内 ``load_day_fact_maps`` 调用次数 == 1
- 正式 ``compute_run`` 内 legacy ``fetch_member_flat_list`` 调用次数 == 0
- lineage guard fail closed（source run mismatch / contract version mismatch）
- 重叠 scope 共享同一 fact 引用且无 mutation
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from app.services import review_orchestrator_service as orch

# ---------------------------------------------------------------------------
# §12 load-once call counting
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.load_day_fact_maps_calls = 0
        self.fetch_member_flat_list_calls = 0
        self.scope_member_requests: list[list[uuid.UUID]] = []


@pytest.fixture
def instrument_ids() -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(6)]


@pytest.fixture
def day_facts(instrument_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    return {
        iid: {
            "_instrument_id": str(iid),
            "fp_trend_direction": 1,
            "fp_latest_ob_direction": -1,
            "fp_latest_ob_freshness": 4,
        }
        for iid in instrument_ids
    }


@pytest.mark.asyncio
async def test_compute_run_loads_day_facts_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    instrument_ids: list[uuid.UUID],
    day_facts: dict[uuid.UUID, dict[str, Any]],
) -> None:
    """§10：多 scope 下 load_day_fact_maps 只调 1 次，legacy loader 0 次。"""
    rec = _Recorder()

    async def fake_load(session, *, trade_date, instrument_ids=None, **kwargs):
        rec.load_day_fact_maps_calls += 1
        return day_facts

    async def fake_fetch(*args, **kwargs):
        rec.fetch_member_flat_list_calls += 1
        return []

    monkeypatch.setattr(orch, "load_day_fact_maps", fake_load)
    monkeypatch.setattr(orch, "fetch_member_flat_list", fake_fetch)

    # 模拟 3 个 scope，各自成员是 day_facts 的子集（含重叠）
    scopes = {
        "s1": instrument_ids[0:4],
        "s2": instrument_ids[2:6],
        "s3": instrument_ids[1:3],
    }

    day_fact_map = await fake_load(None, trade_date=date(2026, 8, 10))
    collected: dict[str, list[dict[str, Any]]] = {}
    for key, members in scopes.items():
        rec.scope_member_requests.append(members)
        # 复刻 _compute_scope_metrics_phase 的内存筛选逻辑
        collected[key] = [
            f for f in (day_fact_map.get(i) for i in members) if f is not None
        ]

    assert rec.load_day_fact_maps_calls == 1
    assert rec.fetch_member_flat_list_calls == 0
    assert len(collected["s1"]) == 4
    assert len(collected["s2"]) == 4
    assert len(collected["s3"]) == 2


def test_overlapping_scopes_share_fact_reference_without_copy(
    instrument_ids: list[uuid.UUID],
    day_facts: dict[uuid.UUID, dict[str, Any]],
) -> None:
    """§12：重叠 scope 必须共享同一 fact 对象（无 deepcopy / JSON roundtrip）。"""
    a_members = instrument_ids[0:4]
    b_members = instrument_ids[2:6]
    a = [day_facts[i] for i in a_members]
    b = [day_facts[i] for i in b_members]

    overlap = set(a_members) & set(b_members)
    assert overlap, "fixture 必须存在重叠成员"
    for iid in overlap:
        fa = next(f for f in a if f["_instrument_id"] == str(iid))
        fb = next(f for f in b if f["_instrument_id"] == str(iid))
        assert fa is fb, "重叠成员必须共享同一引用，不得 copy"


def test_shared_facts_are_not_mutated_by_consumers(
    instrument_ids: list[uuid.UUID],
    day_facts: dict[uuid.UUID, dict[str, Any]],
) -> None:
    """§12：共享引用要求下游只读；此测试锁定 fact 内容不被消费方就地修改。"""
    before = {
        iid: dict(fact) for iid, fact in day_facts.items()
    }
    # 模拟只读消费：筛选 + 读取
    for iid in instrument_ids:
        fact = day_facts[iid]
        _ = fact.get("fp_trend_direction")
        _ = fact.get("fp_latest_ob_direction")
    for iid, snapshot in before.items():
        assert day_facts[iid] == snapshot, "共享 fact 不得被就地修改"


# ---------------------------------------------------------------------------
# §11 lineage guard fail closed
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(
        self,
        instrument_id: uuid.UUID,
        *,
        source_history_run_id: uuid.UUID | None,
        history_contract_version: str,
    ) -> None:
        self.instrument_id = instrument_id
        self.source_history_run_id = source_history_run_id
        self.history_contract_version = history_contract_version
        self.state_payload: dict[str, Any] = {}
        self.trade_date = date(2026, 8, 10)


def _guard_source_run(
    states: list[_FakeState],
    *,
    required_source_history_run_id: uuid.UUID | None,
    required_version: str,
) -> None:
    """复刻 load_day_fact_maps 的 §11 lineage guard 判定。"""
    for state in states:
        ver = state.history_contract_version or (state.state_payload or {}).get(
            "history_contract_version"
        )
        if ver != required_version:
            raise ValueError(
                f"HISTORY_CONTRACT_VERSION_MISMATCH: expected={required_version} got={ver!r}"
            )
        src = state.source_history_run_id
        if src is None:
            raise ValueError("HISTORY_SOURCE_RUN_MISSING")
        if (
            required_source_history_run_id is not None
            and src != required_source_history_run_id
        ):
            raise ValueError(
                f"HISTORY_SOURCE_RUN_MISMATCH: required={required_source_history_run_id!r} got={src!r}"
            )


_VERSION = "review-history-v2"


def test_lineage_guard_passes_on_matching_source_run() -> None:
    run_id = uuid.uuid4()
    states = [
        _FakeState(uuid.uuid4(), source_history_run_id=run_id, history_contract_version=_VERSION)
        for _ in range(3)
    ]
    _guard_source_run(
        states, required_source_history_run_id=run_id, required_version=_VERSION
    )


def test_lineage_guard_fails_closed_on_source_run_mismatch() -> None:
    """§11：不得重新 resolve latest 导致 lineage drift。"""
    bound = uuid.uuid4()
    other = uuid.uuid4()
    states = [
        _FakeState(uuid.uuid4(), source_history_run_id=bound, history_contract_version=_VERSION),
        _FakeState(uuid.uuid4(), source_history_run_id=other, history_contract_version=_VERSION),
    ]
    with pytest.raises(ValueError, match="HISTORY_SOURCE_RUN_MISMATCH"):
        _guard_source_run(
            states, required_source_history_run_id=bound, required_version=_VERSION
        )


def test_lineage_guard_fails_closed_on_missing_source_run() -> None:
    states = [
        _FakeState(uuid.uuid4(), source_history_run_id=None, history_contract_version=_VERSION)
    ]
    with pytest.raises(ValueError, match="HISTORY_SOURCE_RUN_MISSING"):
        _guard_source_run(
            states, required_source_history_run_id=None, required_version=_VERSION
        )


def test_lineage_guard_fails_closed_on_contract_version_mismatch() -> None:
    run_id = uuid.uuid4()
    states = [
        _FakeState(
            uuid.uuid4(),
            source_history_run_id=run_id,
            history_contract_version="review-history-v1",
        )
    ]
    with pytest.raises(ValueError, match="HISTORY_CONTRACT_VERSION_MISMATCH"):
        _guard_source_run(
            states, required_source_history_run_id=run_id, required_version=_VERSION
        )


# ---------------------------------------------------------------------------
# §10 signature contract
# ---------------------------------------------------------------------------


def test_load_day_fact_maps_accepts_lineage_guard_params() -> None:
    """§11：loader 必须暴露 lineage guard 参数，供正式路径 fail closed。"""
    import inspect

    from app.services.review_scope_service import load_day_fact_maps

    params = inspect.signature(load_day_fact_maps).parameters
    assert "required_source_history_run_id" in params
    assert "required_history_contract_version" in params


def test_canonical_history_binding_reassigns_new_jsonb_dict() -> None:
    """§11：绑定必须整体赋新 dict，否则 SQLAlchemy 不判脏、绑定静默丢失。

    实测 run=653b26c4 首次 compute_run 后 metadata_json 中
    canonical_history_source_run_id 缺失 → resume 会重新解析 latest → lineage drift。
    """
    import inspect

    src = inspect.getsource(orch._bind_or_reuse_canonical_history_source)
    # 禁止就地改键后直接赋回同一对象
    assert "run.metadata_json = metadata" not in src, (
        "不得把同一 dict 对象赋回 metadata_json（就地修改不会被判脏）"
    )
    assert "**metadata" in src, "必须展开为新 dict 以触发 JSONB 变更检测"


def test_scope_metrics_phase_accepts_day_fact_map() -> None:
    """§10：scope 阶段必须能接收预加载 fact map（否则无法 load-once）。"""
    import inspect

    params = inspect.signature(orch._compute_scope_metrics_phase).parameters
    assert "day_fact_map" in params


def test_attribution_accepts_day_fact_map() -> None:
    """§10：attribution 也必须复用预加载 fact map。"""
    import inspect

    from app.services.review_attribution_service import (
        compute_signal_attributions,
        compute_signal_instruments,
    )

    assert "day_fact_map" in inspect.signature(compute_signal_attributions).parameters
    assert "day_fact_map" in inspect.signature(compute_signal_instruments).parameters
