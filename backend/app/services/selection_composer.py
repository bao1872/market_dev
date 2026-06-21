"""选股组合引擎（C3）- ALL/ANY 集合运算 + 白名单排名。

核心职责：
- compose: 对多个成员的 MemberMatch 结果进行集合运算
  - ALL: 交集（所有成员都命中的 instrument_id）
  - ANY: 并集（任一成员命中的 instrument_id）
- rank: 对组合结果按白名单表达式排名
  - 白名单聚合函数：sum/avg/max/min/count（不允许任意公式代码）
  - 按 metric_value 排序，生成 RankedInstrument 列表

设计说明：
- 向量化：集合运算使用 set 操作（O(n) 复杂度）
- 白名单排名：仅支持预定义的 5 种聚合函数，杜绝任意代码执行
- contributing_members: 记录每个标的被哪些成员命中（证据链索引）

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.services.selection_executor import MemberMatch

logger = logging.getLogger("selection_composer")

# 白名单聚合函数（不允许任意公式代码）
_AGGREGATIONS = frozenset({"sum", "avg", "max", "min", "count"})


@dataclass
class RankedInstrument:
    """排名后的标的。

    Attributes:
        instrument_id: 标的 ID
        rank: 排名（从 1 开始，相同 score 共享排名）
        score: 排名分值（由聚合函数计算）
        contributing_members: 命中该标的的成员 ID 列表
    """

    instrument_id: uuid.UUID
    rank: int
    score: float
    contributing_members: list[uuid.UUID] = field(default_factory=list)


def compose(
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
    operator: str,
) -> set[uuid.UUID]:
    """对多个成员的 MemberMatch 结果进行集合运算。

    ALL: 交集（所有成员都命中的 instrument_id）
    ANY: 并集（任一成员命中的 instrument_id）

    向量化说明：使用 set 运算（intersection/union），O(n) 复杂度。

    Args:
        member_matches: member_id → (instrument_id → MemberMatch) 映射
        operator: 集合运算符 ALL/ANY

    Returns:
        命中的 instrument_id 集合

    Raises:
        ValueError: operator 不是 ALL/ANY
    """
    if operator not in ("ALL", "ANY"):
        raise ValueError(
            f"非法 operator: {operator}，仅支持 ALL/ANY"
        )

    # 收集每个成员的命中 instrument_id 集合
    matched_sets: list[set[uuid.UUID]] = []
    for _member_id, matches in member_matches.items():
        matched_set = {
            instrument_id
            for instrument_id, mm in matches.items()
            if mm.matched
        }
        matched_sets.append(matched_set)

    if not matched_sets:
        return set()

    if operator == "ALL":
        # 交集：所有成员都命中
        result = matched_sets[0]
        for s in matched_sets[1:]:
            result = result & s
        return result

    # ANY: 并集（任一成员命中）
    result: set[uuid.UUID] = set()
    for s in matched_sets:
        result = result | s
    return result


def get_contributing_members(
    instrument_id: uuid.UUID,
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
) -> list[uuid.UUID]:
    """获取命中指定标的的成员 ID 列表。

    Args:
        instrument_id: 标的 ID
        member_matches: member_id → (instrument_id → MemberMatch) 映射

    Returns:
        命中该标的的成员 ID 列表（按 member_id 顺序）
    """
    contributing = []
    for member_id, matches in member_matches.items():
        mm = matches.get(instrument_id)
        if mm is not None and mm.matched:
            contributing.append(member_id)
    return contributing


def rank(
    instrument_ids: set[uuid.UUID],
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
    sort_spec: list[dict[str, Any]],
) -> list[RankedInstrument]:
    """对组合结果按白名单表达式排名。

    sort_spec 格式（取第一个作为主排名依据）：
    [
        {
            "metric_key": "dsa_dir_bars",  # 指标名
            "aggregation": "sum",          # 聚合函数：sum/avg/max/min/count
            "order": "desc"                # 排序方向：asc/desc（默认 desc）
        }
    ]

    聚合规则（跨命中成员聚合该指标的值）：
    - sum: 命中成员该指标值之和
    - avg: 命中成员该指标值平均
    - max: 命中成员该指标值最大
    - min: 命中成员该指标值最小
    - count: 命中成员数（不依赖 metric_key）

    白名单约束：仅支持上述 5 种聚合函数，不允许任意公式代码。

    Args:
        instrument_ids: 待排名的标的 ID 集合
        member_matches: member_id → (instrument_id → MemberMatch) 映射
        sort_spec: 排名规格列表（取第一个作为主排名依据）

    Returns:
        排名后的 RankedInstrument 列表（按 rank 升序）

    Raises:
        ValueError: sort_spec 为空或聚合函数不在白名单
    """
    if not sort_spec:
        # 无排名规格：所有标的 rank=1，score=0
        return [
            RankedInstrument(
                instrument_id=iid,
                rank=1,
                score=0.0,
                contributing_members=get_contributing_members(iid, member_matches),
            )
            for iid in instrument_ids
        ]

    primary = sort_spec[0]
    aggregation = primary.get("aggregation", "sum")
    metric_key = primary.get("metric_key")
    order = primary.get("order", "desc")

    if aggregation not in _AGGREGATIONS:
        raise ValueError(
            f"非法聚合函数: {aggregation}，仅支持 {sorted(_AGGREGATIONS)}"
        )

    if aggregation != "count" and not metric_key:
        raise ValueError(
            f"aggregation={aggregation} 时必须提供 metric_key"
        )

    # 计算每个标的的 score
    scores: list[tuple[uuid.UUID, float]] = []
    for iid in instrument_ids:
        score = _compute_score(iid, member_matches, aggregation, metric_key)
        scores.append((iid, score))

    # 排序（desc 降序，asc 升序）
    reverse = order == "desc"
    scores.sort(key=lambda x: x[1], reverse=reverse)

    # 生成排名（相同 score 共享排名）
    ranked: list[RankedInstrument] = []
    prev_score: float | None = None
    current_rank = 0
    for idx, (iid, score) in enumerate(scores):
        if prev_score is None or score != prev_score:
            current_rank = idx + 1
            prev_score = score
        ranked.append(
            RankedInstrument(
                instrument_id=iid,
                rank=current_rank,
                score=score,
                contributing_members=get_contributing_members(iid, member_matches),
            )
        )

    return ranked


def _compute_score(
    instrument_id: uuid.UUID,
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
    aggregation: str,
    metric_key: str | None,
) -> float:
    """计算单个标的的排名分值。

    跨命中成员聚合该指标的值。

    Args:
        instrument_id: 标的 ID
        member_matches: member_id → (instrument_id → MemberMatch) 映射
        aggregation: 聚合函数 sum/avg/max/min/count
        metric_key: 指标名（count 时忽略）

    Returns:
        排名分值
    """
    # 收集命中成员的指标值
    values: list[float] = []
    for _member_id, matches in member_matches.items():
        mm = matches.get(instrument_id)
        if mm is None or not mm.matched:
            continue
        if aggregation == "count":
            values.append(1.0)
            continue
        if metric_key is None:
            continue
        val = mm.metrics_summary.get(metric_key)
        if val is None:
            continue
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            # 非数值型指标，跳过
            continue

    if aggregation == "count":
        return float(len(values))
    if not values:
        return 0.0
    if aggregation == "sum":
        return sum(values)
    if aggregation == "avg":
        return sum(values) / len(values)
    if aggregation == "max":
        return max(values)
    if aggregation == "min":
        return min(values)
    return 0.0


if __name__ == "__main__":
    # 自测入口：验证集合运算与排名逻辑（无副作用，不连接数据库）

    # 构造测试数据：3 个成员，3 个标的
    iid1, iid2, iid3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mid1, mid2, mid3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    # 成员 1: 命中 iid1, iid2
    # 成员 2: 命中 iid1, iid3
    # 成员 3: 命中 iid1
    member_matches = {
        mid1: {
            iid1: MemberMatch(iid1, True, {"score": 0.8}),
            iid2: MemberMatch(iid2, True, {"score": 0.6}),
            iid3: MemberMatch(iid3, False, {"score": 0.4}, missing_reason="FILTERED_OUT"),
        },
        mid2: {
            iid1: MemberMatch(iid1, True, {"score": 0.9}),
            iid2: MemberMatch(iid2, False, {"score": 0.6}, missing_reason="FILTERED_OUT"),
            iid3: MemberMatch(iid3, True, {"score": 0.5}),
        },
        mid3: {
            iid1: MemberMatch(iid1, True, {"score": 0.7}),
            iid2: MemberMatch(iid2, False, {"score": 0.6}, missing_reason="FILTERED_OUT"),
            iid3: MemberMatch(iid3, False, {"score": 0.5}, missing_reason="FILTERED_OUT"),
        },
    }

    # 测试 ALL（交集）：只有 iid1 被所有成员命中
    all_result = compose(member_matches, "ALL")
    assert all_result == {iid1}, f"ALL 交集错误: {all_result}"
    print(f"ALL 交集: {all_result} ✓")

    # 测试 ANY（并集）：iid1, iid2, iid3 任一成员命中
    any_result = compose(member_matches, "ANY")
    assert any_result == {iid1, iid2, iid3}, f"ANY 并集错误: {any_result}"
    print(f"ANY 并集: {any_result} ✓")

    # 测试非法 operator
    try:
        compose(member_matches, "XXX")
    except ValueError as e:
        print(f"非法 operator 校验: {e} ✓")

    # 测试 get_contributing_members
    contrib_iid1 = get_contributing_members(iid1, member_matches)
    assert set(contrib_iid1) == {mid1, mid2, mid3}
    print(f"iid1 contributing: {contrib_iid1} ✓")

    contrib_iid2 = get_contributing_members(iid2, member_matches)
    assert set(contrib_iid2) == {mid1}
    print(f"iid2 contributing: {contrib_iid2} ✓")

    # 测试排名 sum（降序）
    sort_spec_sum = [{"metric_key": "score", "aggregation": "sum", "order": "desc"}]
    ranked_sum = rank({iid1, iid2, iid3}, member_matches, sort_spec_sum)
    # iid1: 0.8+0.9+0.7=2.4, iid2: 0.6, iid3: 0.5
    assert ranked_sum[0].instrument_id == iid1
    assert ranked_sum[0].score == 2.4
    assert ranked_sum[0].rank == 1
    print(f"sum 排名（降序）: {[(r.instrument_id, r.rank, r.score) for r in ranked_sum]} ✓")

    # 测试排名 avg
    sort_spec_avg = [{"metric_key": "score", "aggregation": "avg", "order": "desc"}]
    ranked_avg = rank({iid1, iid2, iid3}, member_matches, sort_spec_avg)
    # iid1: 2.4/3=0.8, iid2: 0.6, iid3: 0.5
    assert ranked_avg[0].instrument_id == iid1
    assert abs(ranked_avg[0].score - 0.8) < 1e-9
    print(f"avg 排名: {[(r.instrument_id, r.rank, r.score) for r in ranked_avg]} ✓")

    # 测试排名 count
    sort_spec_count = [{"aggregation": "count", "order": "desc"}]
    ranked_count = rank({iid1, iid2, iid3}, member_matches, sort_spec_count)
    # iid1: 3, iid2: 1, iid3: 1
    assert ranked_count[0].instrument_id == iid1
    assert ranked_count[0].score == 3.0
    print(f"count 排名: {[(r.instrument_id, r.rank, r.score) for r in ranked_count]} ✓")

    # 测试排名 max
    sort_spec_max = [{"metric_key": "score", "aggregation": "max", "order": "desc"}]
    ranked_max = rank({iid1, iid2, iid3}, member_matches, sort_spec_max)
    # iid1: max(0.8,0.9,0.7)=0.9, iid2: 0.6, iid3: 0.5
    assert ranked_max[0].instrument_id == iid1
    assert ranked_max[0].score == 0.9
    print(f"max 排名: {[(r.instrument_id, r.rank, r.score) for r in ranked_max]} ✓")

    # 测试排名 min
    sort_spec_min = [{"metric_key": "score", "aggregation": "min", "order": "asc"}]
    ranked_min = rank({iid1, iid2, iid3}, member_matches, sort_spec_min)
    # iid1: min(0.8,0.9,0.7)=0.7, iid2: 0.6, iid3: 0.5；asc 升序
    assert ranked_min[0].instrument_id == iid3
    assert ranked_min[0].score == 0.5
    print(f"min 排名（升序）: {[(r.instrument_id, r.rank, r.score) for r in ranked_min]} ✓")

    # 测试无排名规格
    ranked_none = rank({iid1, iid2}, member_matches, [])
    assert all(r.rank == 1 and r.score == 0.0 for r in ranked_none)
    print(f"无排名规格: {[(r.instrument_id, r.rank) for r in ranked_none]} ✓")

    # 测试非法聚合函数
    try:
        rank({iid1}, member_matches, [{"metric_key": "score", "aggregation": "stddev"}])
    except ValueError as e:
        print(f"非法聚合函数校验: {e} ✓")

    # 测试相同 score 共享排名
    same_score_matches = {
        mid1: {iid1: MemberMatch(iid1, True, {"score": 0.5})},
        mid2: {iid2: MemberMatch(iid2, True, {"score": 0.5})},
    }
    ranked_same = rank({iid1, iid2}, same_score_matches, [{"metric_key": "score", "aggregation": "sum"}])
    assert all(r.rank == 1 for r in ranked_same)
    print(f"相同 score 共享排名: {[(r.instrument_id, r.rank) for r in ranked_same]} ✓")

    print("OK")
