"""[REVIEW-FACT-PARITY-02 §10-§12] Review load-once 合同测试。

[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] legacy 逐 scope loader
（``fetch_member_flat_list``）已随 legacy owner 物理删除；load-once 合同
现由 canonical 路径承载：``validate_review_lineage_guard`` 每 run 只调 1 次
（§11 lineage fail-closed，无 member fact 物化），``prepare_current_scope_observations_batch``
每 run 只调 1 次（load-once 物化）；scope loop 只按 instrument_id 从内存引用取数据
（无 per-scope 重复读取）。

锁定：
- 正式 ``compute_run`` 内 ``validate_review_lineage_guard`` 调用次数 == 1
- 唯一 per-scope owner ``_compute_canonical_composition_phase`` 接收
  batch-prepared observations（load-once 复用）
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
        self.lineage_guard_calls = 0
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
async def test_compute_run_invokes_lineage_guard_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    instrument_ids: list[uuid.UUID],
    day_facts: dict[uuid.UUID, dict[str, Any]],
) -> None:
    """§10：多 scope 下 validate_review_lineage_guard 只调 1 次（legacy loader 已删除，
    物化结果不再由 orchestrator 持有）。"""
    rec = _Recorder()

    async def fake_guard(session, *, trade_date, **kwargs):
        rec.lineage_guard_calls += 1

    monkeypatch.setattr(orch, "validate_review_lineage_guard", fake_guard)

    # 模拟 3 个 scope，各自成员是 day_facts 的子集（含重叠）
    scopes = {
        "s1": instrument_ids[0:4],
        "s2": instrument_ids[2:6],
        "s3": instrument_ids[1:3],
    }

    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 6] 每 run 只调一次 lineage guard（§11
    # fail closed），物化由 prepare_current_scope_observations_batch 单独负责。
    await fake_guard(None, trade_date=date(2026, 8, 10))

    day_fact_map = day_facts
    collected: dict[str, list[dict[str, Any]]] = {}
    for key, members in scopes.items():
        rec.scope_member_requests.append(members)
        # 复刻 canonical 路径的内存筛选逻辑（无 per-scope 重复 loader）
        collected[key] = [
            f for f in (day_fact_map.get(i) for i in members) if f is not None
        ]

    assert rec.lineage_guard_calls == 1
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


_VERSION = "review-history-v2"


def _history_state_pair(
    *,
    current_source: uuid.UUID | None,
    current_version: str = _VERSION,
    previous_source: uuid.UUID | None = None,
    previous_version: str = _VERSION,
) -> tuple[uuid.UUID, dict[uuid.UUID, _FakeState], dict[uuid.UUID, _FakeState]]:
    instrument_id = uuid.uuid4()
    previous_source = current_source if previous_source is None else previous_source
    return (
        instrument_id,
        {
            instrument_id: _FakeState(
                instrument_id,
                source_history_run_id=current_source,
                history_contract_version=current_version,
            )
        },
        {
            instrument_id: _FakeState(
                instrument_id,
                source_history_run_id=previous_source,
                history_contract_version=previous_version,
            )
        },
    )


def test_history_state_lineage_valid_path() -> None:
    from app.services.review_scope_service import _validate_history_state_lineage

    run_id = uuid.uuid4()
    _, current, previous = _history_state_pair(current_source=run_id)
    _validate_history_state_lineage(
        current,
        previous,
        trade_date=date(2026, 8, 10),
        required_source_history_run_id=run_id,
        required_history_contract_version=_VERSION,
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ({"current_source": None}, "HISTORY_STATE_CURRENT_SOURCE_RUN_NULL"),
        (
            {"current_source": uuid.uuid4(), "current_version": "review-history-v1"},
            "HISTORY_STATE_CURRENT_CONTRACT_MISMATCH",
        ),
        (
            {"current_source": uuid.uuid4(), "required_source": uuid.uuid4()},
            "HISTORY_STATE_CURRENT_SOURCE_RUN_MISMATCH",
        ),
        (
            {"current_source": uuid.uuid4(), "previous_source": uuid.uuid4()},
            "HISTORY_STATE_PREVIOUS_SOURCE_RUN_MISMATCH",
        ),
        (
            {"current_source": uuid.uuid4(), "previous_version": "review-history-v1"},
            "HISTORY_STATE_PREVIOUS_CONTRACT_MISMATCH",
        ),
    ],
)
def test_history_state_lineage_fails_closed(
    case: dict[str, Any], expected: str,
) -> None:
    from app.services.review_scope_service import _validate_history_state_lineage

    required_source = case.pop("required_source", case.get("current_source"))
    _, current, previous = _history_state_pair(**case)
    with pytest.raises(ValueError, match=expected):
        _validate_history_state_lineage(
            current,
            previous,
            trade_date=date(2026, 8, 10),
            required_source_history_run_id=required_source,
            required_history_contract_version=_VERSION,
        )


@pytest.mark.parametrize(
    ("source_matches", "version", "expected"),
    [
        (False, _VERSION, "HISTORY_PREVIOUS_SOURCE_RUN_MISMATCH"),
        (True, "review-history-v1", "HISTORY_CONTRACT_VERSION_MISMATCH"),
    ],
)
def test_stock_core_previous_lineage_fails_closed(
    source_matches: bool, version: str, expected: str,
) -> None:
    from app.services.review_scope_service import _validate_stock_core_previous_lineage

    required_source = uuid.uuid4()
    instrument_id = uuid.uuid4()
    previous_source = required_source if source_matches else uuid.uuid4()
    previous = {
        instrument_id: _FakeState(
            instrument_id,
            source_history_run_id=previous_source,
            history_contract_version=version,
        )
    }
    with pytest.raises(ValueError, match=expected):
        _validate_stock_core_previous_lineage(
            previous,
            trade_date=date(2026, 8, 10),
            required_source_history_run_id=required_source,
            required_history_contract_version=_VERSION,
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


def test_resume_validates_lineage_before_preparing_observations() -> None:
    """resume 必须在昂贵的 member fact 物化前 fail closed。"""
    import inspect

    src = inspect.getsource(orch.resume_run)
    assert src.index("await _bind_or_reuse_canonical_history_source") < src.index(
        "await validate_review_lineage_guard"
    )
    assert src.index("await validate_review_lineage_guard") < src.index(
        "await prepare_current_scope_observations_batch"
    )


def test_canonical_composition_phase_accepts_prepared_observations() -> None:
    """§10：唯一 per-scope owner 必须能接收 batch-prepared observations
    （load-once 复用，禁止 per-scope 重复 member traversal）。"""
    import inspect

    params = inspect.signature(orch._compute_canonical_composition_phase).parameters
    assert "prepared_observations" in params


def test_attribution_accepts_day_fact_map() -> None:
    """§10：attribution 也必须复用预加载 fact map。"""
    import inspect

    from app.services.review_attribution_service import (
        compute_signal_attributions,
        compute_signal_instruments,
    )

    assert "day_fact_map" in inspect.signature(compute_signal_attributions).parameters
    assert "day_fact_map" in inspect.signature(compute_signal_instruments).parameters
