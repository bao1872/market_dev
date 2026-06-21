"""C3 选股组合引擎测试 + C2/C4 集成测试。

测试内容：
1. ALL 交集（compose）
2. ANY 并集（compose）
3. 排名（rank，含 sum/avg/max/min/count 白名单聚合）
4. 多成员方案 + 证据链（execute_member → compose → rank 全流程）
5. 幂等（idempotency_key 计算 + 相同输入不重复）

测试策略：
- compose/rank 为纯函数，直接测试（无需 DB）
- execute_member 使用 mock ORM 对象测试条件评估逻辑
- 幂等键计算为纯函数，直接测试
- 证据链构建逻辑通过 mock MemberMatch 数据验证

覆盖主逻辑 + 边界条件：
- 主逻辑：ALL/ANY 集合运算、5 种聚合函数排名
- 边界：空成员、空结果、相同 score 共享排名、非法 operator/聚合函数
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from app.services.selection_composer import (
    RankedInstrument,
    compose,
    get_contributing_members,
    rank,
)
from app.services.selection_executor import (
    REASON_FILTERED_OUT,
    REASON_NO_RESULT,
    MemberMatch,
    _evaluate_conditions,
    _evaluate_single_condition,
    _extract_metrics,
)
from app.services.selection_run_service import (
    _apply_missing_member_policy,
    _build_result_summary,
    _collect_all_instruments,
    _compute_idempotency_key,
    _compute_input_run_set_hash,
)

# ============================================================
# 测试夹具：构造 mock 成员与 MemberMatch
# ============================================================


class MockCondition:
    """mock SelectionMemberCondition 用于条件评估测试。"""

    def __init__(
        self,
        metric_key: str,
        operator: str,
        value1: Any,
        value2: Any | None = None,
    ) -> None:
        self.metric_key = metric_key
        self.operator = operator
        self.value1 = value1
        self.value2 = value2
        self.member_id = uuid.uuid4()
        self.position = 0


class MockMember:
    """mock SelectionPlanMember 用于成员执行测试。"""

    def __init__(
        self,
        member_id: uuid.UUID | None = None,
        strategy_version_id: uuid.UUID | None = None,
        enabled: bool = True,
        position: int = 0,
        conditions: list[MockCondition] | None = None,
    ) -> None:
        self.id = member_id or uuid.uuid4()
        self.strategy_version_id = strategy_version_id
        self.enabled = enabled
        self.position = position
        self.conditions = conditions or []


@pytest.fixture
def sample_member_matches() -> dict:
    """构造 3 成员 × 3 标的的 MemberMatch 测试数据。

    成员 1: 命中 iid1, iid2
    成员 2: 命中 iid1, iid3
    成员 3: 命中 iid1

    ALL 交集预期: {iid1}
    ANY 并集预期: {iid1, iid2, iid3}
    """
    iid1, iid2, iid3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mid1, mid2, mid3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    return {
        "instrument_ids": {"iid1": iid1, "iid2": iid2, "iid3": iid3},
        "member_ids": {"mid1": mid1, "mid2": mid2, "mid3": mid3},
        "matches": {
            mid1: {
                iid1: MemberMatch(iid1, True, {"score": 0.8, "dsa_dir_bars": 60}),
                iid2: MemberMatch(iid2, True, {"score": 0.6, "dsa_dir_bars": 30}),
                iid3: MemberMatch(
                    iid3, False, {"score": 0.4}, missing_reason=REASON_FILTERED_OUT
                ),
            },
            mid2: {
                iid1: MemberMatch(iid1, True, {"score": 0.9, "dsa_dir_bars": 70}),
                iid2: MemberMatch(
                    iid2, False, {"score": 0.6}, missing_reason=REASON_FILTERED_OUT
                ),
                iid3: MemberMatch(iid3, True, {"score": 0.5, "dsa_dir_bars": 40}),
            },
            mid3: {
                iid1: MemberMatch(iid1, True, {"score": 0.7, "dsa_dir_bars": 50}),
                iid2: MemberMatch(
                    iid2, False, {"score": 0.6}, missing_reason=REASON_FILTERED_OUT
                ),
                iid3: MemberMatch(
                    iid3, False, {"score": 0.5}, missing_reason=REASON_FILTERED_OUT
                ),
            },
        },
    }


# ============================================================
# 1. ALL 交集测试
# ============================================================


def test_compose_all_intersection(sample_member_matches: dict) -> None:
    """测试 ALL 交集：所有成员都命中的 instrument_id。"""
    matches = sample_member_matches["matches"]
    iid1 = sample_member_matches["instrument_ids"]["iid1"]

    result = compose(matches, "ALL")
    assert result == {iid1}, f"ALL 交集应只有 iid1（所有成员都命中）: {result}"


def test_compose_all_single_member() -> None:
    """测试 ALL 单成员：等于该成员的命中集合。"""
    iid1, iid2 = uuid.uuid4(), uuid.uuid4()
    mid1 = uuid.uuid4()
    matches = {
        mid1: {
            iid1: MemberMatch(iid1, True),
            iid2: MemberMatch(iid2, False, missing_reason=REASON_FILTERED_OUT),
        }
    }
    result = compose(matches, "ALL")
    assert result == {iid1}


def test_compose_all_empty_members() -> None:
    """测试 ALL 空成员：返回空集合。"""
    result = compose({}, "ALL")
    assert result == set()


def test_compose_all_no_matches() -> None:
    """测试 ALL 所有成员都无命中：返回空集合。"""
    iid1 = uuid.uuid4()
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    matches = {
        mid1: {iid1: MemberMatch(iid1, False, missing_reason=REASON_FILTERED_OUT)},
        mid2: {iid1: MemberMatch(iid1, False, missing_reason=REASON_FILTERED_OUT)},
    }
    result = compose(matches, "ALL")
    assert result == set()


# ============================================================
# 2. ANY 并集测试
# ============================================================


def test_compose_any_union(sample_member_matches: dict) -> None:
    """测试 ANY 并集：任一成员命中的 instrument_id。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]

    result = compose(matches, "ANY")
    assert result == {iids["iid1"], iids["iid2"], iids["iid3"]}


def test_compose_any_single_member() -> None:
    """测试 ANY 单成员：等于该成员的命中集合。"""
    iid1, iid2 = uuid.uuid4(), uuid.uuid4()
    mid1 = uuid.uuid4()
    matches = {
        mid1: {
            iid1: MemberMatch(iid1, True),
            iid2: MemberMatch(iid2, False, missing_reason=REASON_FILTERED_OUT),
        }
    }
    result = compose(matches, "ANY")
    assert result == {iid1}


def test_compose_any_empty_members() -> None:
    """测试 ANY 空成员：返回空集合。"""
    result = compose({}, "ANY")
    assert result == set()


def test_compose_invalid_operator() -> None:
    """测试非法 operator 抛出 ValueError。"""
    with pytest.raises(ValueError, match="仅支持 ALL/ANY"):
        compose({}, "XXX")


# ============================================================
# 3. 排名测试
# ============================================================


def test_rank_sum_desc(sample_member_matches: dict) -> None:
    """测试 sum 聚合降序排名。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"], iids["iid3"]}

    sort_spec = [{"metric_key": "score", "aggregation": "sum", "order": "desc"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1: 0.8+0.9+0.7=2.4, iid2: 0.6, iid3: 0.5
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == pytest.approx(2.4)
    assert ranked[0].rank == 1
    assert ranked[1].instrument_id == iids["iid2"]
    assert ranked[1].rank == 2
    assert ranked[2].instrument_id == iids["iid3"]
    assert ranked[2].rank == 3


def test_rank_avg_desc(sample_member_matches: dict) -> None:
    """测试 avg 聚合降序排名。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"], iids["iid3"]}

    sort_spec = [{"metric_key": "score", "aggregation": "avg", "order": "desc"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1: 2.4/3=0.8, iid2: 0.6, iid3: 0.5
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == pytest.approx(0.8)


def test_rank_max_desc(sample_member_matches: dict) -> None:
    """测试 max 聚合降序排名。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"], iids["iid3"]}

    sort_spec = [{"metric_key": "score", "aggregation": "max", "order": "desc"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1: max(0.8,0.9,0.7)=0.9, iid2: 0.6, iid3: 0.5
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == pytest.approx(0.9)


def test_rank_min_asc(sample_member_matches: dict) -> None:
    """测试 min 聚合升序排名。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"], iids["iid3"]}

    sort_spec = [{"metric_key": "score", "aggregation": "min", "order": "asc"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1: min(0.8,0.9,0.7)=0.7, iid2: 0.6, iid3: 0.5；asc 升序
    assert ranked[0].instrument_id == iids["iid3"]
    assert ranked[0].score == pytest.approx(0.5)
    assert ranked[2].instrument_id == iids["iid1"]
    assert ranked[2].score == pytest.approx(0.7)


def test_rank_count_desc(sample_member_matches: dict) -> None:
    """测试 count 聚合降序排名（不依赖 metric_key）。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"], iids["iid3"]}

    sort_spec = [{"aggregation": "count", "order": "desc"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1: 3 个成员命中, iid2: 1, iid3: 1
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == 3.0
    # iid2 和 iid3 都是 1，共享排名 2
    assert ranked[1].rank == 2
    assert ranked[2].rank == 2


def test_rank_no_sort_spec(sample_member_matches: dict) -> None:
    """测试无排名规格：所有标的 rank=1, score=0。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    all_iids = {iids["iid1"], iids["iid2"]}

    ranked = rank(all_iids, matches, [])
    assert all(r.rank == 1 and r.score == 0.0 for r in ranked)
    assert len(ranked) == 2


def test_rank_invalid_aggregation() -> None:
    """测试非法聚合函数抛出 ValueError。"""
    iid1 = uuid.uuid4()
    with pytest.raises(ValueError, match="非法聚合函数"):
        rank({iid1}, {}, [{"metric_key": "score", "aggregation": "stddev"}])


def test_rank_missing_metric_key() -> None:
    """测试非 count 聚合缺少 metric_key 抛出 ValueError。"""
    iid1 = uuid.uuid4()
    with pytest.raises(ValueError, match="必须提供 metric_key"):
        rank({iid1}, {}, [{"aggregation": "sum"}])


def test_rank_same_score_shared_rank() -> None:
    """测试相同 score 共享排名。"""
    iid1, iid2 = uuid.uuid4(), uuid.uuid4()
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    matches = {
        mid1: {iid1: MemberMatch(iid1, True, {"score": 0.5})},
        mid2: {iid2: MemberMatch(iid2, True, {"score": 0.5})},
    }
    ranked = rank({iid1, iid2}, matches, [{"metric_key": "score", "aggregation": "sum"}])
    assert all(r.rank == 1 for r in ranked)
    assert all(r.score == 0.5 for r in ranked)


def test_rank_contributing_members(sample_member_matches: dict) -> None:
    """测试排名结果包含 contributing_members。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    mids = sample_member_matches["member_ids"]
    all_iids = {iids["iid1"]}

    sort_spec = [{"metric_key": "score", "aggregation": "sum"}]
    ranked = rank(all_iids, matches, sort_spec)

    # iid1 被所有 3 个成员命中
    assert len(ranked) == 1
    assert set(ranked[0].contributing_members) == {mids["mid1"], mids["mid2"], mids["mid3"]}


# ============================================================
# 4. 条件评估测试（C2 execute_member 的核心逻辑）
# ============================================================


def test_extract_metrics_excludes_matched() -> None:
    """测试指标提取排除 matched 字段。"""
    payload = {"matched": True, "dsa_dir_bars": 60, "offset_percentile": 0.05}
    metrics = _extract_metrics(payload)
    assert "matched" not in metrics
    assert metrics["dsa_dir_bars"] == 60
    assert metrics["offset_percentile"] == 0.05


def test_extract_metrics_empty_payload() -> None:
    """测试空 payload 提取返回空字典。"""
    assert _extract_metrics({}) == {}
    assert _extract_metrics(None) == {}  # type: ignore[arg-type]


def test_evaluate_single_condition_gte() -> None:
    """测试 gte 条件评估。"""
    cond = MockCondition("dsa_dir_bars", "gte", 5)
    assert _evaluate_single_condition(cond, 60) is True
    assert _evaluate_single_condition(cond, 5) is True
    assert _evaluate_single_condition(cond, 4) is False


def test_evaluate_single_condition_lte() -> None:
    """测试 lte 条件评估。"""
    cond = MockCondition("offset_percentile", "lte", 0.8)
    assert _evaluate_single_condition(cond, 0.05) is True
    assert _evaluate_single_condition(cond, 0.8) is True
    assert _evaluate_single_condition(cond, 0.9) is False


def test_evaluate_single_condition_between() -> None:
    """测试 between 条件评估。"""
    cond = MockCondition("score", "between", 0.5, 0.9)
    assert _evaluate_single_condition(cond, 0.7) is True
    assert _evaluate_single_condition(cond, 0.5) is True
    assert _evaluate_single_condition(cond, 0.9) is True
    assert _evaluate_single_condition(cond, 0.4) is False
    assert _evaluate_single_condition(cond, 1.0) is False


def test_evaluate_single_condition_between_no_upper() -> None:
    """测试 between 缺少 value2 时条件未通过。"""
    cond = MockCondition("score", "between", 0.5, None)
    assert _evaluate_single_condition(cond, 0.7) is False


def test_evaluate_single_condition_eq() -> None:
    """测试 eq 条件评估。"""
    cond = MockCondition("category", "eq", "A")
    assert _evaluate_single_condition(cond, "A") is True
    assert _evaluate_single_condition(cond, "B") is False


def test_evaluate_single_condition_type_mismatch() -> None:
    """测试类型不匹配时条件未通过。"""
    cond = MockCondition("score", "gte", 5)
    # 字符串无法转 float
    assert _evaluate_single_condition(cond, "abc") is False


def test_evaluate_conditions_all_pass() -> None:
    """测试 AND 条件全通过。"""
    conditions = [
        MockCondition("dsa_dir_bars", "gte", 5),
        MockCondition("offset_percentile", "lte", 0.8),
    ]
    metrics = {"dsa_dir_bars": 60, "offset_percentile": 0.05}
    matched, reason = _evaluate_conditions(conditions, metrics)
    assert matched is True
    assert reason is None


def test_evaluate_conditions_first_fails() -> None:
    """测试 AND 条件第一个未通过。"""
    conditions = [
        MockCondition("dsa_dir_bars", "gte", 5),
        MockCondition("offset_percentile", "lte", 0.8),
    ]
    metrics = {"dsa_dir_bars": 3, "offset_percentile": 0.05}
    matched, reason = _evaluate_conditions(conditions, metrics)
    assert matched is False
    assert reason == REASON_FILTERED_OUT


def test_evaluate_conditions_metric_missing() -> None:
    """测试 AND 条件指标缺失。"""
    conditions = [MockCondition("dsa_dir_bars", "gte", 5)]
    metrics = {"offset_percentile": 0.05}  # 缺少 dsa_dir_bars
    matched, reason = _evaluate_conditions(conditions, metrics)
    assert matched is False
    assert reason == REASON_FILTERED_OUT


def test_evaluate_conditions_no_conditions() -> None:
    """测试无条件：策略有结果即命中。"""
    matched, reason = _evaluate_conditions([], {"any": 1})
    assert matched is True
    assert reason is None


# ============================================================
# 5. 幂等键测试
# ============================================================


def test_idempotency_key_deterministic() -> None:
    """测试相同输入产生相同幂等键。"""
    rev_id = uuid.uuid4()
    td = date(2026, 6, 18)
    run_set_hash = "abc123def456abcd"

    key1 = _compute_idempotency_key(rev_id, td, "manual", run_set_hash)
    key2 = _compute_idempotency_key(rev_id, td, "manual", run_set_hash)
    assert key1 == key2
    assert len(key1) == 16


def test_idempotency_key_different_trigger_kind() -> None:
    """测试不同 trigger_kind 产生不同幂等键。"""
    rev_id = uuid.uuid4()
    td = date(2026, 6, 18)
    run_set_hash = "abc123def456abcd"

    key_manual = _compute_idempotency_key(rev_id, td, "manual", run_set_hash)
    key_scheduled = _compute_idempotency_key(rev_id, td, "scheduled", run_set_hash)
    assert key_manual != key_scheduled


def test_idempotency_key_different_trade_date() -> None:
    """测试不同 trade_date 产生不同幂等键。"""
    rev_id = uuid.uuid4()
    run_set_hash = "abc123def456abcd"

    key1 = _compute_idempotency_key(rev_id, date(2026, 6, 18), "manual", run_set_hash)
    key2 = _compute_idempotency_key(rev_id, date(2026, 6, 19), "manual", run_set_hash)
    assert key1 != key2


def test_idempotency_key_different_revision() -> None:
    """测试不同 revision_id 产生不同幂等键。"""
    td = date(2026, 6, 18)
    run_set_hash = "abc123def456abcd"

    key1 = _compute_idempotency_key(uuid.uuid4(), td, "manual", run_set_hash)
    key2 = _compute_idempotency_key(uuid.uuid4(), td, "manual", run_set_hash)
    assert key1 != key2


def test_idempotency_key_different_run_set() -> None:
    """测试不同 input_run_set_hash 产生不同幂等键。"""
    rev_id = uuid.uuid4()
    td = date(2026, 6, 18)

    key1 = _compute_idempotency_key(rev_id, td, "manual", "hash1")
    key2 = _compute_idempotency_key(rev_id, td, "manual", "hash2")
    assert key1 != key2


def test_input_run_set_hash_order_independent() -> None:
    """测试 input_run_set_hash 与成员顺序无关。"""
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    vid1, vid2 = uuid.uuid4(), uuid.uuid4()

    members_a = [MockMember(mid1, vid1), MockMember(mid2, vid2)]
    members_b = [MockMember(mid2, vid2), MockMember(mid1, vid1)]  # 顺序不同

    hash_a = _compute_input_run_set_hash(members_a)
    hash_b = _compute_input_run_set_hash(members_b)
    assert hash_a == hash_b


def test_input_run_set_hash_none_version() -> None:
    """测试 None strategy_version_id 的哈希。"""
    mid1 = uuid.uuid4()
    vid1 = uuid.uuid4()

    members_with_version = [MockMember(mid1, vid1)]
    members_none = [MockMember(mid1, None)]

    hash_with = _compute_input_run_set_hash(members_with_version)
    hash_none = _compute_input_run_set_hash(members_none)
    assert hash_with != hash_none


# ============================================================
# 6. missing_member_policy 测试
# ============================================================


def test_missing_member_policy_ignore_member() -> None:
    """测试 IGNORE_MEMBER 策略：空成员不抛异常。"""
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    iid1 = uuid.uuid4()
    member_matches = {
        mid1: {iid1: MemberMatch(iid1, True)},
        mid2: {},  # 空结果
    }
    # IGNORE_MEMBER 不应抛异常
    _apply_missing_member_policy(member_matches, [], "IGNORE_MEMBER")


def test_missing_member_policy_fail_closed() -> None:
    """测试 FAIL_CLOSED 策略：空成员抛 ValueError。"""
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    iid1 = uuid.uuid4()
    member_matches = {
        mid1: {iid1: MemberMatch(iid1, True)},
        mid2: {},  # 空结果
    }

    class MockEnabledMember:
        def __init__(self, mid, enabled=True, position=0):
            self.id = mid
            self.enabled = enabled
            self.position = position

    members = [MockEnabledMember(mid1), MockEnabledMember(mid2)]
    with pytest.raises(ValueError, match="FAIL_CLOSED"):
        _apply_missing_member_policy(member_matches, members, "FAIL_CLOSED")


def test_missing_member_policy_fail_closed_disabled_member() -> None:
    """测试 FAIL_CLOSED 策略：未启用成员不触发失败。"""
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    iid1 = uuid.uuid4()
    member_matches = {
        mid1: {iid1: MemberMatch(iid1, True)},
        mid2: {},  # 空结果但成员未启用
    }

    class MockEnabledMember:
        def __init__(self, mid, enabled=True, position=0):
            self.id = mid
            self.enabled = enabled
            self.position = position

    members = [MockEnabledMember(mid1), MockEnabledMember(mid2, enabled=False)]
    # mid2 未启用，不触发 FAIL_CLOSED
    _apply_missing_member_policy(member_matches, members, "FAIL_CLOSED")


# ============================================================
# 7. 证据链构建测试
# ============================================================


def test_collect_all_instruments(sample_member_matches: dict) -> None:
    """测试收集所有出现过的 instrument_id（含命中与未命中）。"""
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]

    all_ids = _collect_all_instruments(matches)
    assert all_ids == {iids["iid1"], iids["iid2"], iids["iid3"]}


def test_get_contributing_members_all_hit(sample_member_matches: dict) -> None:
    """测试获取命中标的的成员列表（所有成员命中）。"""
    matches = sample_member_matches["matches"]
    mids = sample_member_matches["member_ids"]
    iid1 = sample_member_matches["instrument_ids"]["iid1"]

    contributing = get_contributing_members(iid1, matches)
    assert set(contributing) == {mids["mid1"], mids["mid2"], mids["mid3"]}


def test_get_contributing_members_partial_hit(sample_member_matches: dict) -> None:
    """测试获取命中标的的成员列表（部分成员命中）。"""
    matches = sample_member_matches["matches"]
    mids = sample_member_matches["member_ids"]
    iid2 = sample_member_matches["instrument_ids"]["iid2"]

    # iid2 只被 mid1 命中
    contributing = get_contributing_members(iid2, matches)
    assert set(contributing) == {mids["mid1"]}


def test_build_result_summary(sample_member_matches: dict) -> None:
    """测试构建结果摘要：包含命中成员的指标快照。"""
    matches = sample_member_matches["matches"]
    mids = sample_member_matches["member_ids"]
    iid1 = sample_member_matches["instrument_ids"]["iid1"]

    summary = _build_result_summary(iid1, matches)
    # iid1 被所有 3 个成员命中，summary 应包含 3 个成员的指标
    assert str(mids["mid1"]) in summary
    assert str(mids["mid2"]) in summary
    assert str(mids["mid3"]) in summary
    assert summary[str(mids["mid1"])]["score"] == 0.8


def test_build_result_summary_no_hits() -> None:
    """测试构建结果摘要：无命中成员时为空字典。"""
    iid1 = uuid.uuid4()
    mid1 = uuid.uuid4()
    matches = {
        mid1: {iid1: MemberMatch(iid1, False, missing_reason=REASON_FILTERED_OUT)},
    }
    summary = _build_result_summary(iid1, matches)
    assert summary == {}


# ============================================================
# 8. 多成员方案全流程测试（execute → compose → rank）
# ============================================================


def test_full_pipeline_all_operator(sample_member_matches: dict) -> None:
    """测试多成员方案全流程（ALL 操作符）。

    流程：member_matches → compose(ALL) → rank
    验证：最终结果只含所有成员都命中的标的，排名正确。
    """
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]

    # 1. 组合（ALL 交集）
    composed = compose(matches, "ALL")
    assert composed == {iids["iid1"]}

    # 2. 排名
    sort_spec = [{"metric_key": "score", "aggregation": "sum", "order": "desc"}]
    ranked = rank(composed, matches, sort_spec)

    # 3. 验证结果
    assert len(ranked) == 1
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == pytest.approx(2.4)  # 0.8+0.9+0.7
    assert ranked[0].rank == 1
    # contributing_members 应包含所有 3 个成员
    assert len(ranked[0].contributing_members) == 3


def test_full_pipeline_any_operator(sample_member_matches: dict) -> None:
    """测试多成员方案全流程（ANY 操作符）。

    流程：member_matches → compose(ANY) → rank
    验证：最终结果含任一成员命中的标的，排名正确。
    """
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]

    # 1. 组合（ANY 并集）
    composed = compose(matches, "ANY")
    assert composed == {iids["iid1"], iids["iid2"], iids["iid3"]}

    # 2. 排名（按 count 降序）
    sort_spec = [{"aggregation": "count", "order": "desc"}]
    ranked = rank(composed, matches, sort_spec)

    # 3. 验证结果
    assert len(ranked) == 3
    # iid1 命中数最多（3），应排第一
    assert ranked[0].instrument_id == iids["iid1"]
    assert ranked[0].score == 3.0


def test_full_pipeline_with_evidence_chain(sample_member_matches: dict) -> None:
    """测试多成员方案 + 证据链。

    验证：每个标的的 contributing_members 正确反映命中成员，
    未命中成员的证据（missing_reason）在 MemberMatch 中保留。
    """
    matches = sample_member_matches["matches"]
    iids = sample_member_matches["instrument_ids"]
    mids = sample_member_matches["member_ids"]

    # ALL 交集
    composed = compose(matches, "ALL")
    assert composed == {iids["iid1"]}

    # 验证 iid1 的证据链：所有成员都命中
    iid1_matches = {
        mid: matches[mid][iids["iid1"]] for mid in matches
    }
    assert all(mm.matched for mm in iid1_matches.values())
    assert all(mm.missing_reason is None for mm in iid1_matches.values())

    # 验证 iid2 的证据链：只有 mid1 命中
    iid2_matches = {
        mid: matches[mid].get(iids["iid2"]) for mid in matches
    }
    assert iid2_matches[mids["mid1"]].matched is True
    assert iid2_matches[mids["mid2"]].matched is False
    assert iid2_matches[mids["mid2"]].missing_reason == REASON_FILTERED_OUT

    # 验证 iid3 的证据链：只有 mid2 命中
    iid3_matches = {
        mid: matches[mid].get(iids["iid3"]) for mid in matches
    }
    assert iid3_matches[mids["mid2"]].matched is True
    assert iid3_matches[mids["mid1"]].matched is False
    assert iid3_matches[mids["mid1"]].missing_reason == REASON_FILTERED_OUT


def test_full_pipeline_empty_results() -> None:
    """测试边界：所有成员都无结果。"""
    mid1 = uuid.uuid4()
    matches = {mid1: {}}

    # ALL 和 ANY 都应返回空集合
    assert compose(matches, "ALL") == set()
    assert compose(matches, "ANY") == set()

    # 排名空集合
    ranked = rank(set(), matches, [{"metric_key": "score", "aggregation": "sum"}])
    assert ranked == []


def test_member_match_dataclass() -> None:
    """测试 MemberMatch dataclass 字段。"""
    iid = uuid.uuid4()
    rid = uuid.uuid4()
    mm = MemberMatch(
        instrument_id=iid,
        matched=True,
        metrics_summary={"score": 0.8},
        result_id=rid,
        missing_reason=None,
    )
    assert mm.instrument_id == iid
    assert mm.matched is True
    assert mm.metrics_summary == {"score": 0.8}
    assert mm.result_id == rid
    assert mm.missing_reason is None

    # 未命中的 MemberMatch
    mm_unmatched = MemberMatch(
        instrument_id=iid,
        matched=False,
        metrics_summary={},
        result_id=None,
        missing_reason=REASON_NO_RESULT,
    )
    assert mm_unmatched.matched is False
    assert mm_unmatched.result_id is None
    assert mm_unmatched.missing_reason == REASON_NO_RESULT


def test_ranked_instrument_dataclass() -> None:
    """测试 RankedInstrument dataclass 字段。"""
    iid = uuid.uuid4()
    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    ri = RankedInstrument(
        instrument_id=iid,
        rank=1,
        score=2.5,
        contributing_members=[mid1, mid2],
    )
    assert ri.instrument_id == iid
    assert ri.rank == 1
    assert ri.score == 2.5
    assert ri.contributing_members == [mid1, mid2]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
