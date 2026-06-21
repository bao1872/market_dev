"""C7 事件关联状态机测试 - Task 26.5。

测试内容：
1. INDEPENDENT 模式：每个成员独立触发，直接生成 CompositeEvent（member_count=1）
2. ANY 模式 + VETO：第一个符合事件确认即触发；VETO 优先处理取消等待
3. ALL 模式 + 窗口 + 顺序 + VETO + 超时：
   - TRIGGER 打开窗口
   - CONFIRM 在窗口内确认
   - 全部确认后生成 CompositeEvent
   - VETO 取消窗口
   - 超时未确认则状态转为 expired
   - ordered=true 时按 position 顺序校验
4. event_time 驱动（非墙钟）：所有时间判断基于 event_time，不使用 datetime.now()

测试策略：
- 纯函数测试：correlate_event 是纯函数，不依赖 DB
- 构造 StrategyEvent/MonitoringPlanState/MonitoringPlanRevision/MonitoringPlanMember 实例
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.models.monitoring_plan import MonitoringPlanMember, MonitoringPlanRevision
from app.models.monitoring_plan_state import MonitoringPlanState
from app.models.strategy_event import StrategyEvent
from app.services.monitoring_correlator import (
    EVENT_TYPE_ALL_CONFIRMED,
    EVENT_TYPE_ANY,
    EVENT_TYPE_INDEPENDENT,
    EVENT_TYPE_VETOED,
    STATUS_COOLDOWN,
    STATUS_EXPIRED,
    STATUS_VETOED,
    STATUS_WAITING_CONFIRM,
    STATUS_WAITING_TRIGGER,
    build_composite_event_key,
    correlate_event,
)


@pytest.fixture
def test_ids() -> dict[str, uuid.UUID]:
    """测试用 UUID 集合。"""
    return {
        "user_id": uuid.uuid4(),
        "plan_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "instrument_id": uuid.uuid4(),
        "strategy_def_id": uuid.uuid4(),
        "strategy_ver_id": uuid.uuid4(),
    }


def _make_revision(
    ids: dict[str, uuid.UUID],
    mode: str = "INDEPENDENT",
    confirmation_window_seconds: int = 0,
    ordered: bool = False,
    cooldown_seconds: int = 600,
) -> MonitoringPlanRevision:
    """构造测试用 MonitoringPlanRevision。"""
    return MonitoringPlanRevision(
        id=ids["revision_id"],
        monitoring_plan_id=ids["plan_id"],
        revision=1,
        mode=mode,
        confirmation_window_seconds=confirmation_window_seconds,
        ordered=ordered,
        cooldown_seconds=cooldown_seconds,
        process_event_policy="IN_APP_ONLY",
        notification_config={},
        created_by=ids["user_id"],
    )


def _make_member(
    ids: dict[str, uuid.UUID],
    *,
    event_type: str = "evt_test",
    role: str = "TRIGGER",
    position: int = 0,
    required: bool = True,
    enabled: bool = True,
    version_policy: str = "PINNED",
    strategy_version_id: uuid.UUID | None = None,
) -> MonitoringPlanMember:
    """构造测试用 MonitoringPlanMember。"""
    return MonitoringPlanMember(
        id=uuid.uuid4(),
        revision_id=ids["revision_id"],
        strategy_definition_id=ids["strategy_def_id"],
        strategy_version_id=strategy_version_id or ids["strategy_ver_id"],
        version_policy=version_policy,
        event_type=event_type,
        role=role,
        position=position,
        required=required,
        enabled=enabled,
        params={},
        conditions=[],
    )


def _make_state(
    ids: dict[str, uuid.UUID],
    *,
    status: str = STATUS_WAITING_TRIGGER,
    window_started_at: datetime | None = None,
    window_deadline_at: datetime | None = None,
    cooldown_until: datetime | None = None,
    confirmed_member_ids: list[uuid.UUID] | None = None,
    lock_version: int = 0,
) -> MonitoringPlanState:
    """构造测试用 MonitoringPlanState。"""
    return MonitoringPlanState(
        id=uuid.uuid4(),
        user_id=ids["user_id"],
        monitoring_plan_id=ids["plan_id"],
        revision_id=ids["revision_id"],
        instrument_id=ids["instrument_id"],
        status=status,
        window_started_at=window_started_at,
        window_deadline_at=window_deadline_at,
        cooldown_until=cooldown_until,
        confirmed_member_ids=confirmed_member_ids or [],
        state_payload={},
        lock_version=lock_version,
    )


def _make_event(
    ids: dict[str, uuid.UUID],
    *,
    event_type: str = "evt_test",
    event_time: datetime | None = None,
    event_key: str = "k1",
) -> StrategyEvent:
    """构造测试用 StrategyEvent。"""
    return StrategyEvent(
        id=uuid.uuid4(),
        event_key=event_key,
        strategy_version_id=ids["strategy_ver_id"],
        instrument_id=ids["instrument_id"],
        event_type=event_type,
        event_time=event_time or datetime(2026, 6, 18, 10, 30, 0),
        schema_version=1,
        payload={"x": 1},
        snapshot={},
    )


class TestIndependentMode:
    """INDEPENDENT 模式测试。"""

    def test_independent_triggers_composite_event(self, test_ids: dict[str, uuid.UUID]) -> None:
        """INDEPENDENT 模式：成员事件直接生成组合事件。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT")
        member = _make_member(test_ids, role="TRIGGER", position=0)
        state = _make_state(test_ids)
        event = _make_event(test_ids)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert result.accepted
        assert result.composite_event is not None
        assert result.composite_event.event_type == EVENT_TYPE_INDEPENDENT
        assert result.composite_event.member_count == 1
        assert result.composite_event.user_id == test_ids["user_id"]
        assert result.composite_event.revision_id == test_ids["revision_id"]
        assert result.new_state["new_status"] == STATUS_COOLDOWN

    def test_independent_no_matching_member(self, test_ids: dict[str, uuid.UUID]) -> None:
        """INDEPENDENT 模式：事件无匹配成员时拒绝。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT")
        member = _make_member(test_ids, event_type="evt_other", role="TRIGGER")
        state = _make_state(test_ids)
        event = _make_event(test_ids, event_type="evt_test")

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert not result.accepted
        assert result.composite_event is None

    def test_independent_observe_role_no_trigger(self, test_ids: dict[str, uuid.UUID]) -> None:
        """INDEPENDENT 模式：OBSERVE 角色不触发组合事件。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT")
        member = _make_member(test_ids, role="OBSERVE")
        state = _make_state(test_ids)
        event = _make_event(test_ids)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert result.accepted
        assert result.composite_event is None

    def test_independent_cooldown_blocks_event(self, test_ids: dict[str, uuid.UUID]) -> None:
        """INDEPENDENT 模式：冷却中拒绝事件。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT", cooldown_seconds=600)
        member = _make_member(test_ids, role="TRIGGER")
        # 冷却截止时间在未来
        state = _make_state(
            test_ids,
            status=STATUS_COOLDOWN,
            cooldown_until=datetime(2026, 6, 18, 11, 0, 0),
        )
        event = _make_event(test_ids, event_time=datetime(2026, 6, 18, 10, 30, 0))

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert not result.accepted
        assert "冷却中" in result.reason


class TestAnyMode:
    """ANY 模式测试。"""

    def test_any_first_event_triggers(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ANY 模式：第一个符合事件即触发。"""
        revision = _make_revision(test_ids, mode="ANY")
        member = _make_member(test_ids, role="TRIGGER")
        state = _make_state(test_ids)
        event = _make_event(test_ids)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert result.accepted
        assert result.composite_event is not None
        assert result.composite_event.event_type == EVENT_TYPE_ANY
        assert result.new_state["new_status"] == STATUS_COOLDOWN

    def test_any_veto_cancels(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ANY 模式：VETO 事件取消等待，生成 VETOED 组合事件。"""
        revision = _make_revision(test_ids, mode="ANY")
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        veto_member = _make_member(test_ids, event_type="evt_veto", role="VETO", position=1)
        state = _make_state(test_ids)
        veto_event = _make_event(test_ids, event_type="evt_veto", event_time=datetime(2026, 6, 18, 10, 31, 0))

        result = correlate_event(
            event=veto_event, plan_state=state, revision=revision,
            members=[trigger_member, veto_member], watermark=veto_event.event_time,
        )

        assert result.accepted
        assert result.composite_event is not None
        assert result.composite_event.event_type == EVENT_TYPE_VETOED
        assert result.new_state["new_status"] == STATUS_VETOED
        assert result.new_state["vetoed_by_member_id"] == veto_member.id
        assert result.new_state["clear_window"] is True

    def test_any_observe_no_trigger(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ANY 模式：OBSERVE 角色不触发。"""
        revision = _make_revision(test_ids, mode="ANY")
        member = _make_member(test_ids, role="OBSERVE")
        state = _make_state(test_ids)
        event = _make_event(test_ids)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert result.accepted
        assert result.composite_event is None


class TestAllMode:
    """ALL 模式测试。"""

    def test_all_trigger_opens_window(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：TRIGGER 事件打开窗口。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        state = _make_state(test_ids)
        trigger_event = _make_event(test_ids, event_type="evt_trigger")

        result = correlate_event(
            event=trigger_event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member], watermark=trigger_event.event_time,
        )

        assert result.accepted
        assert result.composite_event is None  # 仅打开窗口，未生成组合事件
        assert result.new_state["new_status"] == STATUS_WAITING_CONFIRM
        assert result.new_state["window_started_at"] == trigger_event.event_time
        assert result.new_state["window_deadline_at"] == trigger_event.event_time + timedelta(seconds=900)
        assert trigger_member.id in result.new_state["confirmed_member_ids"]

    def test_all_confirm_completes(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：所有 required 成员确认后生成组合事件。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        # 模拟 TRIGGER 后的状态
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id],
        )
        confirm_event = _make_event(
            test_ids, event_type="evt_confirm",
            event_time=datetime(2026, 6, 18, 10, 35, 0),
            event_key="k2",
        )

        result = correlate_event(
            event=confirm_event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member], watermark=confirm_event.event_time,
        )

        assert result.accepted
        assert result.composite_event is not None
        assert result.composite_event.event_type == EVENT_TYPE_ALL_CONFIRMED
        assert result.composite_event.member_count == 2
        assert result.new_state["new_status"] == STATUS_COOLDOWN

    def test_all_veto_cancels_window(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：VETO 事件取消窗口。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        veto_member = _make_member(test_ids, event_type="evt_veto", role="VETO", position=1)
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id],
        )
        veto_event = _make_event(
            test_ids, event_type="evt_veto",
            event_time=datetime(2026, 6, 18, 10, 35, 0),
            event_key="k2",
        )

        result = correlate_event(
            event=veto_event, plan_state=state, revision=revision,
            members=[trigger_member, veto_member], watermark=veto_event.event_time,
        )

        assert result.accepted
        assert result.composite_event is not None
        assert result.composite_event.event_type == EVENT_TYPE_VETOED
        assert result.new_state["new_status"] == STATUS_VETOED
        assert result.new_state["vetoed_by_member_id"] == veto_member.id
        assert result.new_state["clear_window"] is True

    def test_all_window_timeout(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：窗口超时未确认则状态转为 expired。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        # 窗口已超时
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id],
        )
        # watermark 超过 deadline
        confirm_event = _make_event(
            test_ids, event_type="evt_confirm",
            event_time=datetime(2026, 6, 18, 10, 50, 0),  # 超过 deadline
            event_key="k2",
        )

        result = correlate_event(
            event=confirm_event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member],
            watermark=datetime(2026, 6, 18, 10, 50, 0),  # watermark 超过 deadline
        )

        assert not result.accepted
        assert result.composite_event is None
        assert result.new_state["new_status"] == STATUS_EXPIRED
        assert "超时" in result.reason

    def test_all_confirm_outside_window(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：CONFIRM 事件晚于窗口截止时间则 expired。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id],
        )
        # CONFIRM 事件晚于 deadline（但 watermark 未超过，避免触发超时分支）
        confirm_event = _make_event(
            test_ids, event_type="evt_confirm",
            event_time=datetime(2026, 6, 18, 10, 46, 0),
            event_key="k2",
        )

        result = correlate_event(
            event=confirm_event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member],
            watermark=datetime(2026, 6, 18, 10, 44, 0),  # watermark 未超过 deadline
        )

        assert not result.accepted
        assert result.composite_event is None
        assert result.new_state["new_status"] == STATUS_EXPIRED

    def test_all_duplicate_confirm_ignored(self, test_ids: dict[str, uuid.UUID]) -> None:
        """ALL 模式：重复 CONFIRM 事件幂等忽略。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id, confirm_member.id],  # 已确认
        )
        confirm_event = _make_event(
            test_ids, event_type="evt_confirm",
            event_time=datetime(2026, 6, 18, 10, 35, 0),
            event_key="k2",
        )

        result = correlate_event(
            event=confirm_event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member], watermark=confirm_event.event_time,
        )

        assert not result.accepted
        assert "已确认" in result.reason

    def test_all_ordered_position_violation(self, test_ids: dict[str.UUID]) -> None:
        """ALL 模式：ordered=true 时顺序不满足则拒绝。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900, ordered=True
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm1 = _make_member(test_ids, event_type="evt_confirm1", role="CONFIRM", position=1)
        confirm2 = _make_member(test_ids, event_type="evt_confirm2", role="CONFIRM", position=2)
        # 已确认 trigger，下一个应为 position=1
        state = _make_state(
            test_ids,
            status=STATUS_WAITING_CONFIRM,
            window_started_at=datetime(2026, 6, 18, 10, 30, 0),
            window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
            confirmed_member_ids=[trigger_member.id],
        )
        # 尝试确认 position=2（应拒绝，应为 position=1）
        event = _make_event(
            test_ids, event_type="evt_confirm2",
            event_time=datetime(2026, 6, 18, 10, 35, 0),
            event_key="k2",
        )

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[trigger_member, confirm1, confirm2], watermark=event.event_time,
        )

        assert not result.accepted
        assert "顺序不满足" in result.reason


class TestEventTimeDriven:
    """event_time 驱动测试（非墙钟）。"""

    def test_window_judgement_based_on_event_time(
        self, test_ids: dict[str, uuid.UUID]
    ) -> None:
        """窗口判断基于 event_time，不依赖 datetime.now()。"""
        revision = _make_revision(
            test_ids, mode="ALL", confirmation_window_seconds=900
        )
        trigger_member = _make_member(test_ids, event_type="evt_trigger", role="TRIGGER", position=0)
        confirm_member = _make_member(test_ids, event_type="evt_confirm", role="CONFIRM", position=1)
        state = _make_state(test_ids)
        # 使用历史时间作为 event_time
        historical_time = datetime(2025, 1, 1, 10, 30, 0)
        event = _make_event(test_ids, event_type="evt_trigger", event_time=historical_time)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[trigger_member, confirm_member], watermark=historical_time,
        )

        assert result.accepted
        # 窗口时间应基于 event_time，而非 datetime.now()
        assert result.new_state["window_started_at"] == historical_time
        assert result.new_state["window_deadline_at"] == historical_time + timedelta(seconds=900)

    def test_cooldown_judgement_based_on_event_time(
        self, test_ids: dict[str, uuid.UUID]
    ) -> None:
        """冷却判断基于 event_time，不依赖 datetime.now()。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT", cooldown_seconds=600)
        member = _make_member(test_ids, role="TRIGGER")
        # 冷却截止时间在 event_time 之后（即冷却未过期）
        event_time = datetime(2026, 6, 18, 10, 30, 0)
        state = _make_state(
            test_ids,
            status=STATUS_COOLDOWN,
            cooldown_until=datetime(2026, 6, 18, 11, 0, 0),  # event_time 之后
        )
        event = _make_event(test_ids, event_time=event_time)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event_time,
        )

        assert not result.accepted
        assert "冷却中" in result.reason


class TestCompositeEventKey:
    """composite_event_key 幂等测试。"""

    def test_key_consistency(self, test_ids: dict[str, uuid.UUID]) -> None:
        """相同输入产生相同 key。"""
        member_ids = [uuid.uuid4(), uuid.uuid4()]
        key1 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=member_ids,
        )
        key2 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=member_ids,
        )
        assert key1 == key2

    def test_key_member_order_invariant(self, test_ids: dict[str, uuid.UUID]) -> None:
        """成员顺序不同应产生相同 key（排序后参与 hash）。"""
        m1 = uuid.uuid4()
        m2 = uuid.uuid4()
        key1 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=[m1, m2],
        )
        key2 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=[m2, m1],  # 顺序不同
        )
        assert key1 == key2

    def test_key_uniqueness(self, test_ids: dict[str, uuid.UUID]) -> None:
        """不同输入产生不同 key。"""
        key1 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test_a",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=[uuid.uuid4()],
        )
        key2 = build_composite_event_key(
            revision_id=test_ids["revision_id"],
            instrument_id=test_ids["instrument_id"],
            event_type="test_b",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            member_ids=[uuid.uuid4()],
        )
        assert key1 != key2


class TestEvidenceFreeze:
    """证据冻结测试。"""

    def test_evidence_contains_frozen_fields(
        self, test_ids: dict[str, uuid.UUID]
    ) -> None:
        """证据应冻结策略版本/事件类型/事件时间/摘要。"""
        revision = _make_revision(test_ids, mode="INDEPENDENT")
        member = _make_member(test_ids, role="TRIGGER")
        state = _make_state(test_ids)
        event = _make_event(test_ids)

        result = correlate_event(
            event=event, plan_state=state, revision=revision,
            members=[member], watermark=event.event_time,
        )

        assert result.composite_event is not None
        assert len(result.composite_event.evidence) == 1
        evidence = result.composite_event.evidence[0]
        # 冻结字段
        assert evidence.strategy_version_id == event.strategy_version_id
        assert evidence.event_type == event.event_type
        assert evidence.event_time == event.event_time
        assert evidence.member_id == member.id
        assert evidence.strategy_event_id == event.id
        # summary 含冻结信息
        assert "strategy_version_id" in evidence.summary
        assert "event_type" in evidence.summary
        assert "event_time" in evidence.summary
        assert "payload_summary" in evidence.summary


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
