"""[V2.1 EPIC-02] board_facts_service 纯函数单元测试。

覆盖不需要 DB 的纯逻辑：
- _stable_snapshot_hash 顺序无关（boards/memberships 乱序 hash 相同）
- 不同快照 hash 不同
- run_mode 校验、历史回放常量

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_board_facts_service.py -q -p no:cacheprovider
"""

from __future__ import annotations

from app.services.board_facts_service import (
    ALL_RUN_MODES,
    RUN_MODE_HISTORICAL_REPLAY,
    RUN_MODE_MANUAL_CURRENT,
    RUN_MODE_SCHEDULED_CURRENT,
    _count_boards_by_level,
    _stable_snapshot_hash,
)


class _FakeSnapshot:
    """最小 BoardSnapshot 兼容对象（boards + memberships）。"""

    def __init__(self, boards, memberships):
        self.boards = boards
        self.memberships = memberships
        self.raw_rows = 0
        self.unresolved_symbols = []


def _snapshot_a():
    boards = [
        {"external_code": "wc:i:aaa", "name": "银行", "type": "industry"},
        {"external_code": "wc:c:bbb", "name": "AI", "type": "concept"},
        {"external_code": "wc:c:ccc", "name": "白酒", "type": "concept"},
    ]
    memberships = {
        ("wc:i:aaa", "industry"): ["600000", "000001"],
        ("wc:c:bbb", "concept"): ["600000"],
        ("wc:c:ccc", "concept"): ["000001", "600519"],
    }
    return _FakeSnapshot(boards, memberships)


def test_snapshot_hash_is_order_independent() -> None:
    """boards/memberships 乱序时 hash 不变（EPIC-02 要求）。"""
    a = _snapshot_a()
    reversed_boards = list(reversed(a.boards))
    reversed_members = {
        k: a.memberships[k] for k in list(reversed(list(a.memberships.keys())))
    }
    b = _FakeSnapshot(reversed_boards, reversed_members)

    assert _stable_snapshot_hash(a) == _stable_snapshot_hash(b)


def test_snapshot_hash_membership_order_independent() -> None:
    """单股 symbol 列表乱序时 hash 不变。"""
    a = _snapshot_a()
    b = _snapshot_a()
    b.memberships[("wc:i:aaa", "industry")] = ["000001", "600000"]  # 乱序
    assert _stable_snapshot_hash(a) == _stable_snapshot_hash(b)


def test_snapshot_hash_differs_for_different_content() -> None:
    """内容不同则 hash 不同。"""
    a = _snapshot_a()
    b = _snapshot_a()
    b.boards.append({"external_code": "wc:c:ddd", "name": "新能源", "type": "concept"})
    assert _stable_snapshot_hash(a) != _stable_snapshot_hash(b)


def test_run_modes_are_stable() -> None:
    """run_mode 常量集合稳定。"""
    assert ALL_RUN_MODES == {
        RUN_MODE_SCHEDULED_CURRENT,
        RUN_MODE_MANUAL_CURRENT,
        RUN_MODE_HISTORICAL_REPLAY,
    }
    assert RUN_MODE_HISTORICAL_REPLAY == "historical_replay"


def test_count_boards_by_level() -> None:
    """行业 L1/L2/L3 层级计数（Commit A §6.2）。"""
    snapshot = _FakeSnapshot(
        boards=[
            {"type": "industry", "hierarchy_level": "L1"},
            {"type": "industry", "hierarchy_level": "L2"},
            {"type": "industry", "hierarchy_level": "L3"},
            {"type": "industry", "hierarchy_level": "L2"},
            {"type": "concept", "hierarchy_level": "L1"},
        ],
        memberships={},
    )
    assert _count_boards_by_level(snapshot, "L1") == 1
    assert _count_boards_by_level(snapshot, "L2") == 2
    assert _count_boards_by_level(snapshot, "L3") == 1


def test_count_boards_by_level_empty() -> None:
    """无 boards 时各层级计数为 0。"""
    snapshot = _FakeSnapshot(boards=[], memberships={})
    assert _count_boards_by_level(snapshot, "L1") == 0
    assert _count_boards_by_level(snapshot, "L2") == 0
    assert _count_boards_by_level(snapshot, "L3") == 0
