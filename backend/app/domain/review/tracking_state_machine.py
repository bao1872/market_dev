"""ReviewTrackingStateMachine - 信号生命周期状态机（PRD §10）。

PRD §10.1 系统信号生命周期：
    new → continuing → confirmed / weakened / invalidated / transformed

规则：
- 同一 scope 同一 signal_type 连续命中：continuing
- 达到 filter 配置中的确认条件：confirmed
- 偏差减弱但尚未失效：weakened
- 达到失效条件：invalidated
- 转为另一信号类型：旧信号 transformed 并关联新信号
- 禁止前端根据颜色自行判断状态

PRD §10.2 用户追踪：
- 用户可追踪 signal / scope / instrument
- 每天 Review Run 完成后自动生成 evaluation
- 用户关闭追踪不删除历史

状态合法值（与 ORM CheckConstraint 一致）：
- 系统信号：new / continuing / confirmed / weakened / invalidated / transformed
- 用户追踪：active / confirmed / invalidated / closed

模块自测：
    python -m app.domain.review.tracking_state_machine
"""

from __future__ import annotations

from typing import Any

# =============================================================================
# 系统信号状态（PRD §10.1）
# =============================================================================

SIGNAL_STATUS_NEW = "new"
SIGNAL_STATUS_CONTINUING = "continuing"
SIGNAL_STATUS_CONFIRMED = "confirmed"
SIGNAL_STATUS_WEAKENED = "weakened"
SIGNAL_STATUS_INVALIDATED = "invalidated"
SIGNAL_STATUS_TRANSFORMED = "transformed"

ALL_SIGNAL_STATUSES: frozenset[str] = frozenset({
    SIGNAL_STATUS_NEW,
    SIGNAL_STATUS_CONTINUING,
    SIGNAL_STATUS_CONFIRMED,
    SIGNAL_STATUS_WEAKENED,
    SIGNAL_STATUS_INVALIDATED,
    SIGNAL_STATUS_TRANSFORMED,
})

# 终态：不再继续演化
TERMINAL_SIGNAL_STATUSES: frozenset[str] = frozenset({
    SIGNAL_STATUS_INVALIDATED,
    SIGNAL_STATUS_TRANSFORMED,
})

# =============================================================================
# 用户追踪状态（PRD §10.2、ORM CheckConstraint）
# =============================================================================

TRACKING_STATUS_ACTIVE = "active"
TRACKING_STATUS_CONFIRMED = "confirmed"
TRACKING_STATUS_INVALIDATED = "invalidated"
TRACKING_STATUS_CLOSED = "closed"

ALL_TRACKING_STATUSES: frozenset[str] = frozenset({
    TRACKING_STATUS_ACTIVE,
    TRACKING_STATUS_CONFIRMED,
    TRACKING_STATUS_INVALIDATED,
    TRACKING_STATUS_CLOSED,
})

TERMINAL_TRACKING_STATUSES: frozenset[str] = frozenset({
    TRACKING_STATUS_CLOSED,
})


# =============================================================================
# 系统信号状态转移
# =============================================================================


def determine_signal_status(
    *,
    is_hit: bool,
    previous_status: str | None,
    previous_signal_id: str | None,
    consecutive_days: int,
    confirmation_rule: dict[str, Any] | None,
    invalidation_rule: dict[str, Any] | None,
    confirmation_conditions_met: bool = False,
    invalidation_conditions_met: bool = False,
    transformed_to_signal_type: str | None = None,
) -> str:
    """根据当前命中状态与历史决定信号生命周期状态（PRD §10.1）。

    Args:
        is_hit: 当前交易日是否命中同一 signal_type
        previous_status: 前一交易日同 scope 同 signal_type 的信号状态
        previous_signal_id: 前一交易日信号 ID
        consecutive_days: 连续命中天数（含今日）
        confirmation_rule: 筛选器的确认规则（来自 FilterDefinition.confirmation_rule）
        invalidation_rule: 筛选器的失效规则
        confirmation_conditions_met: 是否满足确认条件（由 service 层评估）
        invalidation_conditions_met: 是否满足失效条件
        transformed_to_signal_type: 转化为的新 signal_type（None=未转化）

    Returns:
        new / continuing / confirmed / weakened / invalidated / transformed

    状态机规则：
    1. 终态信号不再变化（invalidated / transformed）
    2. 命中且无前序：new
    3. 命中且前序非终态、满足确认条件：confirmed
    4. 命中且前序非终态、连续命中：continuing
    5. 未命中但前序状态为 confirmed/continuing、偏差减弱但未失效：weakened
    6. 未命中且满足失效条件：invalidated
    7. 转化为新 signal_type：transformed
    """
    # 转化优先（PRD §10.1：转为另一信号类型）
    if transformed_to_signal_type is not None:
        return SIGNAL_STATUS_TRANSFORMED

    # 失效条件满足
    if invalidation_conditions_met:
        return SIGNAL_STATUS_INVALIDATED

    if is_hit:
        # 命中：检查确认条件
        if confirmation_conditions_met:
            return SIGNAL_STATUS_CONFIRMED
        if previous_signal_id is None or previous_status is None:
            return SIGNAL_STATUS_NEW
        # 前序为终态不再 continuing
        if previous_status in TERMINAL_SIGNAL_STATUSES:
            # 终态信号再次命中视为 new（新生命周期）
            return SIGNAL_STATUS_NEW
        # 连续命中
        return SIGNAL_STATUS_CONTINUING

    # 未命中：评估是否减弱或失效
    if previous_status in (
        SIGNAL_STATUS_CONFIRMED, SIGNAL_STATUS_CONTINUING, SIGNAL_STATUS_NEW,
        SIGNAL_STATUS_WEAKENED,
    ):
        # 未命中且偏差不再持续 → 检查是否仍接近触发阈值
        # weakened 表示偏差减弱但尚未失效
        if invalidation_conditions_met:
            return SIGNAL_STATUS_INVALIDATED
        return SIGNAL_STATUS_WEAKENED

    # 默认：前序已是终态或无前序，未命中视为无效
    return SIGNAL_STATUS_INVALIDATED


def evaluate_confirmation(
    confirmation_rule: dict[str, Any] | None,
    *,
    consecutive_days: int,
    extra_conditions_met: dict[str, bool] | None = None,
) -> bool:
    """评估是否满足确认条件（PRD §10.1 confirmed 状态）。

    Args:
        confirmation_rule: FilterDefinition.confirmation_rule
        consecutive_days: 连续命中天数
        extra_conditions_met: 额外条件评估结果 {condition_name: met}

    Returns:
        True 表示满足确认条件
    """
    if not confirmation_rule:
        return False
    min_days = confirmation_rule.get("min_consecutive_days")
    if min_days is not None and consecutive_days < min_days:
        return False
    extra_required = confirmation_rule.get("extra_conditions") or []
    if extra_required:
        extras = extra_conditions_met or {}
        for cond in extra_required:
            if not extras.get(cond, False):
                return False
    return True


def evaluate_invalidation(
    invalidation_rule: dict[str, Any] | None,
    *,
    context: dict[str, Any],
) -> bool:
    """评估是否满足失效条件（PRD §10.1 invalidated 状态）。

    Args:
        invalidation_rule: FilterDefinition.invalidation_rule
        context: P/Q/U/C/V payload + coverage

    Returns:
        True 表示满足任一失效条件
    """
    if not invalidation_rule:
        return False
    conditions = invalidation_rule.get("conditions") or []
    if not conditions:
        return False
    # 解析简单条件字符串（如 "Q.delta1d > 10"）
    for cond_str in conditions:
        if _evaluate_condition_string(cond_str, context):
            return True
    return False


def _evaluate_condition_string(cond_str: str, context: dict[str, Any]) -> bool:
    """解析并评估简单条件字符串（如 "Q.delta1d > 10"）。

    支持：>、<、>=、<=、==、!=
    """
    if not isinstance(cond_str, str):
        return False
    # 简单解析：split by 空格
    parts = cond_str.split()
    if len(parts) != 3:
        return False
    field_path, op, value_str = parts
    try:
        threshold = float(value_str)
    except ValueError:
        return False

    # 解析 field_path
    path_parts = field_path.split(".")
    if len(path_parts) == 1:
        v = context.get(path_parts[0])
    elif len(path_parts) == 2:
        metric = context.get(path_parts[0])
        if not isinstance(metric, dict):
            return False
        v = metric.get(path_parts[1])
    else:
        return False

    if v is None:
        return False
    try:
        val = float(v)
    except (TypeError, ValueError):
        return False

    if op == ">":
        return val > threshold
    if op == "<":
        return val < threshold
    if op == ">=":
        return val >= threshold
    if op == "<=":
        return val <= threshold
    if op == "==":
        return abs(val - threshold) < 1e-9
    if op == "!=":
        return abs(val - threshold) >= 1e-9
    return False


# =============================================================================
# 用户追踪状态转移
# =============================================================================


def determine_tracking_status(
    *,
    current_tracking_status: str,
    current_signal_status: str | None,
    confirmation_conditions: dict[str, Any] | None,
    invalidation_conditions: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> str:
    """决定用户追踪的状态（PRD §10.2、§5.7 状态机）。

    Args:
        current_tracking_status: 当前追踪状态（active/confirmed/invalidated/closed）
        current_signal_status: 当前交易日关联信号的状态（None=未关联或未命中）
        confirmation_conditions: 用户自定义确认条件
        invalidation_conditions: 用户自定义失效条件
        context: 评估 context（P/Q/U/C/V payload）

    Returns:
        active / confirmed / invalidated / closed

    规则：
    1. closed 不再变化（终态）
    2. 用户自定义失效条件满足：invalidated
    3. 用户自定义确认条件满足：confirmed
    4. 关联信号 invalidated/transformed：invalidated
    5. 关联信号 confirmed：confirmed
    6. 关联信号 weakened：保持原状态（active 或 confirmed）
    7. 默认保持原状态
    """
    # closed 为终态
    if current_tracking_status == TRACKING_STATUS_CLOSED:
        return TRACKING_STATUS_CLOSED

    # 评估用户自定义失效条件
    if invalidation_conditions and context is not None:
        if evaluate_invalidation(invalidation_conditions, context=context):
            return TRACKING_STATUS_INVALIDATED

    # 评估用户自定义确认条件
    if confirmation_conditions and context is not None:
        # 评估 confirmation_rule
        if evaluate_confirmation(
            confirmation_conditions,
            consecutive_days=int(context.get("_consecutive_days", 0)),
        ):
            return TRACKING_STATUS_CONFIRMED

    # 关联信号状态映射
    if current_signal_status == SIGNAL_STATUS_INVALIDATED:
        return TRACKING_STATUS_INVALIDATED
    if current_signal_status == SIGNAL_STATUS_TRANSFORMED:
        # 转化视为旧追踪失效，用户应基于新信号重新追踪
        return TRACKING_STATUS_INVALIDATED
    if current_signal_status == SIGNAL_STATUS_CONFIRMED:
        return TRACKING_STATUS_CONFIRMED
    # weakened / continuing / new / None：保持原状态
    return current_tracking_status


def compute_duration_days(
    first_seen_date: Any,
    trade_date: Any,
) -> int:
    """计算信号持续日数（trade_date - first_seen_date，按交易日近似为日历日）。

    Args:
        first_seen_date: 信号首次出现日期（date 或 ISO 字符串）
        trade_date: 当前交易日（date 或 ISO 字符串）

    Returns:
        持续日数（>=0；无法解析返回 0）
    """
    from datetime import date as date_cls

    def _parse(d: Any) -> date_cls | None:
        if d is None:
            return None
        if isinstance(d, date_cls):
            return d
        if isinstance(d, str):
            try:
                return date_cls.fromisoformat(d)
            except ValueError:
                return None
        return None

    first = _parse(first_seen_date)
    cur = _parse(trade_date)
    if first is None or cur is None:
        return 0
    delta = (cur - first).days
    return max(0, delta)


if __name__ == "__main__":
    # 自测：信号状态机
    # 1. 新信号
    s = determine_signal_status(
        is_hit=True, previous_status=None, previous_signal_id=None,
        consecutive_days=1, confirmation_rule=None, invalidation_rule=None,
    )
    assert s == SIGNAL_STATUS_NEW

    # 2. 连续命中
    s = determine_signal_status(
        is_hit=True, previous_status=SIGNAL_STATUS_NEW,
        previous_signal_id="prev-id", consecutive_days=2,
        confirmation_rule={"min_consecutive_days": 3},
        invalidation_rule={},
    )
    assert s == SIGNAL_STATUS_CONTINUING

    # 3. 满足确认条件
    s = determine_signal_status(
        is_hit=True, previous_status=SIGNAL_STATUS_CONTINUING,
        previous_signal_id="prev-id", consecutive_days=3,
        confirmation_rule={"min_consecutive_days": 3},
        invalidation_rule={},
        confirmation_conditions_met=True,
    )
    assert s == SIGNAL_STATUS_CONFIRMED

    # 4. 未命中但前序 confirmed → weakened
    s = determine_signal_status(
        is_hit=False, previous_status=SIGNAL_STATUS_CONFIRMED,
        previous_signal_id="prev-id", consecutive_days=3,
        confirmation_rule={}, invalidation_rule={},
    )
    assert s == SIGNAL_STATUS_WEAKENED

    # 5. 满足失效条件
    s = determine_signal_status(
        is_hit=True, previous_status=SIGNAL_STATUS_CONTINUING,
        previous_signal_id="prev-id", consecutive_days=2,
        confirmation_rule={}, invalidation_rule={},
        invalidation_conditions_met=True,
    )
    assert s == SIGNAL_STATUS_INVALIDATED

    # 6. 转化
    s = determine_signal_status(
        is_hit=True, previous_status=SIGNAL_STATUS_CONTINUING,
        previous_signal_id="prev-id", consecutive_days=2,
        confirmation_rule={}, invalidation_rule={},
        transformed_to_signal_type="another_signal_type",
    )
    assert s == SIGNAL_STATUS_TRANSFORMED

    # 7. 追踪状态机
    t = determine_tracking_status(
        current_tracking_status=TRACKING_STATUS_ACTIVE,
        current_signal_status=SIGNAL_STATUS_CONFIRMED,
        confirmation_conditions=None, invalidation_conditions=None,
    )
    assert t == TRACKING_STATUS_CONFIRMED

    t = determine_tracking_status(
        current_tracking_status=TRACKING_STATUS_ACTIVE,
        current_signal_status=SIGNAL_STATUS_INVALIDATED,
        confirmation_conditions=None, invalidation_conditions=None,
    )
    assert t == TRACKING_STATUS_INVALIDATED

    t = determine_tracking_status(
        current_tracking_status=TRACKING_STATUS_CLOSED,
        current_signal_status=None,
        confirmation_conditions=None, invalidation_conditions=None,
    )
    assert t == TRACKING_STATUS_CLOSED

    # 8. 持续日数
    d = compute_duration_days("2026-07-01", "2026-07-10")
    assert d == 9
    print(f"OK: duration_days={d}")
    print("OK: tracking_state_machine verified")
