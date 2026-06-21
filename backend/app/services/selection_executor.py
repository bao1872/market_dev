"""选股成员执行器（C2）- 执行单个成员策略并生成 MemberMatch。

核心职责：
- execute_member: 通过 SQL 端过滤查询成员策略版本在指定交易日的选股结果，
  应用成员 conditions（gt/gte/lt/lte/eq/between）筛选，生成 instrument_id → MemberMatch 映射。

MemberMatch 字段：
- instrument_id: 标的 ID
- matched: 是否命中（通过 SQL 端筛选的结果均为 True）
- metrics_summary: 指标摘要（从策略结果 payload 提取的关键指标快照）
- result_id: 原始策略结果 ID（策略无结果时为 None）
- missing_reason: 缺失原因（命中时为 None）
  - NO_RESULT: 策略无结果（该成员策略在 trade_date 无任何 StrategyResult）
  - DATA_MISSING: 行情缺失（策略版本未解析或无可用版本）

设计说明：
- SQL 端过滤：通过 strategy_result_repository.query_results() 在数据库执行筛选，
  不再将全市场结果加载到 Python 内存后本地过滤
- published_run_id 绑定：查询结果绑定到已发布的 run，确保用户只查询已发布批次
- 条件转换：_conditions_to_filters 将 SelectionMemberCondition 转换为 metric_filters 格式
- 禁异常吞没：查询失败补充上下文后 re-raise
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.selection_plan import SelectionMemberCondition, SelectionPlanMember
from app.repositories.strategy_result_repository import query_results

logger = logging.getLogger("selection_executor")

# 缺失原因枚举（与 selection_result_evidence.reason_code 对齐）
REASON_NO_RESULT = "NO_RESULT"
REASON_DATA_MISSING = "DATA_MISSING"


@dataclass
class MemberMatch:
    """成员对单个标的的命中结果。

    Attributes:
        instrument_id: 标的 ID
        matched: 是否命中（通过 SQL 端筛选的结果均为 True）
        metrics_summary: 指标摘要（从策略结果 payload 提取的关键指标快照）
        result_id: 原始策略结果 ID（策略无结果时为 None）
        missing_reason: 缺失原因（命中时为 None）：
            NO_RESULT/DATA_MISSING
    """

    instrument_id: uuid.UUID
    matched: bool
    metrics_summary: dict[str, Any] = field(default_factory=dict)
    result_id: uuid.UUID | None = None
    missing_reason: str | None = None


async def execute_member(
    db: AsyncSession,
    member: SelectionPlanMember,
    trade_date: date,
    published_run_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, MemberMatch]:
    """执行单个成员策略，通过 SQL 端过滤生成 instrument_id → MemberMatch 映射。

    流程：
    1. 校验成员 strategy_version_id 是否已解析（STABLE_TRACK 可能未解析）
    2. 加载成员 conditions 并转换为 metric_filters
    3. 调用 query_results 进行 SQL 端过滤（绑定 published_run_id）
    4. 生成 MemberMatch（通过筛选的均为 matched=True）

    Args:
        db: 异步数据库会话
        member: 方案成员 ORM 对象（需已加载 conditions 关系）
        trade_date: 交易日
        published_run_id: 已发布的 run_id（绑定 published 批次，None 时按 version_id+trade_date 查询）

    Returns:
        instrument_id → MemberMatch 映射（可能为空字典）

    Raises:
        RuntimeError: 查询策略结果失败时补充上下文后 re-raise
    """
    # 1. 校验策略版本已解析
    if member.strategy_version_id is None:
        logger.warning(
            "成员策略版本未解析（STABLE_TRACK 无 released 版本）: "
            "member_id=%s, position=%d",
            member.id, member.position,
        )
        return {}

    # 2. 加载成员条件（若未加载则查询）
    conditions = list(member.conditions) if member.conditions else []
    if not conditions:
        try:
            cond_stmt = (
                select(SelectionMemberCondition)
                .where(SelectionMemberCondition.member_id == member.id)
                .order_by(SelectionMemberCondition.position)
            )
            cond_result = await db.execute(cond_stmt)
            conditions = list(cond_result.scalars().all())
        except Exception as exc:
            raise RuntimeError(
                f"查询成员条件失败 member_id={member.id}: {exc}"
            ) from exc

    # 3. 转换为 metric_filters 格式
    metric_filters = _conditions_to_filters(conditions)

    # 4. SQL 端过滤查询（绑定 published_run_id）
    try:
        strategy_results = await query_results(
            db,
            run_id=published_run_id,
            strategy_version_id=member.strategy_version_id,
            trade_date=trade_date,
            metric_filters=metric_filters,
            limit=10000,
            offset=0,
        )
    except Exception as exc:
        raise RuntimeError(
            f"SQL 端过滤查询成员策略结果失败 member_id={member.id}, "
            f"strategy_version_id={member.strategy_version_id}, "
            f"trade_date={trade_date}, published_run_id={published_run_id}: {exc}"
        ) from exc

    if not strategy_results:
        logger.info(
            "成员策略无结果（SQL 端过滤后）: member_id=%s, strategy_version_id=%s, "
            "trade_date=%s, published_run_id=%s",
            member.id, member.strategy_version_id, trade_date, published_run_id,
        )
        return {}

    # 5. 构建 MemberMatch（通过 SQL 端筛选的均为 matched=True）
    matches: dict[uuid.UUID, MemberMatch] = {}
    for sr in strategy_results:
        metrics = _extract_metrics(sr.payload)
        matches[sr.instrument_id] = MemberMatch(
            instrument_id=sr.instrument_id,
            matched=True,
            metrics_summary=metrics,
            result_id=sr.id,
            missing_reason=None,
        )

    return matches


def _conditions_to_filters(
    conditions: list[SelectionMemberCondition],
) -> list[dict[str, Any]]:
    """将 SelectionMemberCondition 转换为 metric_filters 格式。

    转换规则：
    - gt/gte/lt/lte/eq: {"metric_key": ..., "operator": ..., "value": value1}
    - between: {"metric_key": ..., "operator": "between", "value1": value1, "value2": value2}
    - SelectionMemberCondition 无 enabled 字段，全部条件参与转换

    Args:
        conditions: 成员条件列表

    Returns:
        metric_filters 格式的筛选条件列表
    """
    filters: list[dict[str, Any]] = []
    for cond in conditions:
        f: dict[str, Any] = {
            "metric_key": cond.metric_key,
            "operator": cond.operator,
        }
        if cond.operator == "between":
            f["value1"] = cond.value1
            f["value2"] = cond.value2
        else:
            f["value"] = cond.value1
        filters.append(f)
    return filters


def _extract_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """从策略结果 payload 中提取指标字典。

    payload 仅包含 metrics（matched 不再持久化），直接返回全部字段。

    Args:
        payload: 策略结果 payload JSONB

    Returns:
        指标字典（key=指标名, value=指标值）
    """
    if not isinstance(payload, dict):
        return {}
    return dict(payload)


if __name__ == "__main__":
    # 自测入口：验证条件转换逻辑（无副作用，不连接数据库）

    # 构造 mock condition
    class MockCondition:
        def __init__(self, metric_key, operator, value1, value2=None):
            self.metric_key = metric_key
            self.operator = operator
            self.value1 = value1
            self.value2 = value2
            self.member_id = uuid.uuid4()

    # 测试 _conditions_to_filters
    conditions = [
        MockCondition("dsa_dir_bars", "gte", 50),
        MockCondition("offset_percentile", "lte", 0.8),
        MockCondition("vwap_ret_avg", "between", 0.0, 0.5),
        MockCondition("regime_value", "eq", 1),
    ]
    filters = _conditions_to_filters(conditions)
    assert len(filters) == 4, f"应有 4 个 filter，实际: {len(filters)}"
    assert filters[0] == {"metric_key": "dsa_dir_bars", "operator": "gte", "value": 50}
    assert filters[1] == {"metric_key": "offset_percentile", "operator": "lte", "value": 0.8}
    assert filters[2] == {"metric_key": "vwap_ret_avg", "operator": "between", "value1": 0.0, "value2": 0.5}
    assert filters[3] == {"metric_key": "regime_value", "operator": "eq", "value": 1}
    print(f"_conditions_to_filters: {filters} ✓")

    # 测试空条件
    assert _conditions_to_filters([]) == []
    print("空条件 → 空 filters ✓")

    # 测试 _extract_metrics（payload 不含 matched）
    payload = {"dsa_dir_bars": 60, "offset_percentile": 0.05}
    metrics = _extract_metrics(payload)
    assert metrics == payload
    print(f"_extract_metrics: {metrics} ✓")

    # 测试 _extract_metrics（非 dict 输入）
    assert _extract_metrics(None) == {}
    assert _extract_metrics("not a dict") == {}
    print("_extract_metrics 非 dict 输入 ✓")

    # 测试 MemberMatch dataclass
    mm = MemberMatch(
        instrument_id=uuid.uuid4(),
        matched=True,
        metrics_summary={"score": 0.8},
        result_id=uuid.uuid4(),
        missing_reason=None,
    )
    assert mm.matched is True
    assert mm.missing_reason is None
    print(f"MemberMatch: {mm} ✓")

    # 验证 execute_member 签名包含 published_run_id 参数
    import inspect

    sig = inspect.signature(execute_member)
    assert "published_run_id" in sig.parameters
    assert sig.parameters["published_run_id"].default is None
    print("execute_member 签名包含 published_run_id ✓")

    print("OK")
