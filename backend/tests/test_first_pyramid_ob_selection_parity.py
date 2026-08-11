"""[REVIEW-FACT-PARITY-02] latest-OB SAME-INPUT parity 合同测试。

锁定 defect：OB_MITIGATED 事件同时携带 ``enter_index``/``enter_time``（用于
``entered_before_mitigation`` 语义）与 ``mitigated_index``/``mitigated_time``。
snapshot 路径旧实现用 ``enter_index or mitigated_index or confirmed_index``
的 ``or`` 链定位事件 bar，会把 MITIGATED 事件错误盖上 **enter bar**，而 history
路径用 type-switch 取 ``mitigated_index``（canonical）。同一 SAME-INPUT 事件集合
因此得到不同 bar_index → latest-OB direction / freshness 分叉。

真实观测（2026-08-10，PIT qfq 500-bar 输入）：
    600880: snapshot freshness=8   vs history freshness=4    （enter bar 491 vs mitigated bar 495）
    603897: snapshot freshness=173 vs history freshness=15   （enter bar 326 vs mitigated bar 484）

本测试用等价 synthetic fixture 锁定修复，禁止回归。
"""

from __future__ import annotations

import pytest

from app.domain.first_pyramid.ob_selection import (
    ob_event_bar_index,
    ob_event_time,
    select_latest_ob,
)


def _ob_created(idx: int, time: str, bias: int) -> dict:
    return {
        "type": "OB_CREATED",
        "bias": bias,
        "confirmed_index": idx,
        "confirmed_time": time,
        "enter_index": None,
        "enter_time": None,
        "mitigated_index": None,
        "mitigated_time": None,
    }


def _ob_mitigated(
    *, mitigated_index: int, mitigated_time: str, enter_index: int, enter_time: str, bias: int
) -> dict:
    """MITIGATED 事件同时携带 enter_* 与 mitigated_*（真实 producer 行为）。"""
    return {
        "type": "OB_MITIGATED",
        "bias": bias,
        "confirmed_index": enter_index - 10,
        "confirmed_time": "2025-01-02",
        "enter_index": enter_index,
        "enter_time": enter_time,
        "mitigated_index": mitigated_index,
        "mitigated_time": mitigated_time,
    }


class TestObEventBarIndexCanonical:
    """canonical 定位：每类事件只认自己的时点字段，禁止 `or` 回退。"""

    def test_created_uses_confirmed_index(self) -> None:
        evt = _ob_created(100, "2025-06-01", bias=1)
        assert ob_event_bar_index(evt) == 100
        assert ob_event_time(evt) == "2025-06-01"

    def test_entered_uses_enter_index(self) -> None:
        evt = {
            "type": "OB_ENTERED",
            "confirmed_index": 80,
            "confirmed_time": "2025-05-01",
            "enter_index": 120,
            "enter_time": "2025-07-01",
        }
        assert ob_event_bar_index(evt) == 120
        assert ob_event_time(evt) == "2025-07-01"

    def test_mitigated_uses_mitigated_index_not_enter_index(self) -> None:
        """核心回归：MITIGATED 必须用 mitigated_index，不得回退到 enter_index。"""
        evt = _ob_mitigated(
            mitigated_index=484,
            mitigated_time="2026-07-17",
            enter_index=326,
            enter_time="2025-11-21",
            bias=1,
        )
        assert ob_event_bar_index(evt) == 484
        assert ob_event_time(evt) == "2026-07-17"

    def test_bar_index_zero_is_not_treated_as_missing(self) -> None:
        """`or` 链的第二类 bug：index==0 是合法 bar，不得被当作缺失。"""
        evt = _ob_mitigated(
            mitigated_index=0,
            mitigated_time="2024-01-02",
            enter_index=5,
            enter_time="2024-01-09",
            bias=-1,
        )
        assert ob_event_bar_index(evt) == 0


class TestCase600880Equivalent:
    """§5-A：600880 等价 fixture — 两条路径 latest OB direction 一致。"""

    LAST_BAR = 499

    def _events(self) -> list[dict]:
        # 一个更早创建的看多 OB，随后一个 mitigated（bias=-1）在 bar 495 发生，
        # 但其 enter bar 在 491（旧 `or` 链会误取 491 → freshness 8）。
        return [
            _ob_created(470, "2026-06-20", bias=1),
            _ob_mitigated(
                mitigated_index=495,
                mitigated_time="2026-08-04",
                enter_index=491,
                enter_time="2026-07-29",
                bias=-1,
            ),
        ]

    def test_latest_ob_direction_and_freshness_match_history(self) -> None:
        events = self._events()
        selected = select_latest_ob(events)
        assert selected is not None
        assert selected["type"] == "OB_MITIGATED"
        assert selected["bias"] == -1
        freshness = self.LAST_BAR - ob_event_bar_index(selected)
        # canonical = 4（mitigated bar 495）；旧 `or` 链会得到 8（enter bar 491）
        assert freshness == 4


class TestCase603897Freshness:
    """§5-B / §4：603897 — 同一 canonical event 下 freshness 一致。"""

    LAST_BAR = 499

    def test_freshness_uses_mitigation_bar(self) -> None:
        events = [
            _ob_created(300, "2025-10-10", bias=1),
            _ob_mitigated(
                mitigated_index=484,
                mitigated_time="2026-07-17",
                enter_index=326,
                enter_time="2025-11-21",
                bias=1,
            ),
        ]
        selected = select_latest_ob(events)
        assert selected is not None
        freshness = self.LAST_BAR - ob_event_bar_index(selected)
        # canonical = 15；旧 `or` 链会得到 173（enter bar 326）
        assert freshness == 15


class TestInputOrderInvariance:
    """§5-C：输入顺序不同但 canonical chronology 相同 → latest OB 一致。"""

    def _events(self) -> list[dict]:
        return [
            _ob_created(120, "2025-03-01", bias=1),
            _ob_mitigated(
                mitigated_index=400,
                mitigated_time="2026-04-01",
                enter_index=150,
                enter_time="2025-04-01",
                bias=-1,
            ),
            _ob_created(300, "2025-11-01", bias=1),
        ]

    def test_shuffled_input_yields_same_latest_ob(self) -> None:
        events = self._events()
        expected = select_latest_ob(events)
        assert expected is not None
        assert ob_event_bar_index(expected) == 400

        for perm in (
            [events[2], events[0], events[1]],
            [events[1], events[2], events[0]],
            list(reversed(events)),
        ):
            got = select_latest_ob(perm)
            assert got is not None
            assert ob_event_bar_index(got) == ob_event_bar_index(expected)
            assert got["type"] == expected["type"]
            assert got["bias"] == expected["bias"]

    def test_same_bar_ties_take_last_appended(self) -> None:
        """同 bar 多事件：取列表最后一个，与 history `>=` 游标语义一致。"""
        a = _ob_created(200, "2025-08-01", bias=1)
        b = _ob_created(200, "2025-08-01", bias=-1)
        assert select_latest_ob([a, b])["bias"] == -1
        assert select_latest_ob([b, a])["bias"] == 1

    def test_events_without_locatable_bar_are_skipped(self) -> None:
        broken = {"type": "OB_MITIGATED", "bias": 1, "mitigated_index": None}
        good = _ob_created(50, "2025-02-01", bias=-1)
        selected = select_latest_ob([good, broken])
        assert selected is good

    def test_empty_input_returns_none(self) -> None:
        assert select_latest_ob([]) is None


class TestControl300675:
    """§5-D：CONTROL — 无 MITIGATED 混淆时行为保持不变。"""

    def test_pure_created_sequence_unchanged(self) -> None:
        events = [
            _ob_created(100, "2025-03-01", bias=1),
            _ob_created(250, "2025-09-01", bias=-1),
            _ob_created(499, "2026-08-10", bias=1),
        ]
        selected = select_latest_ob(events)
        assert selected is not None
        assert ob_event_bar_index(selected) == 499
        assert selected["bias"] == 1
        assert 499 - ob_event_bar_index(selected) == 0


class TestFlattenSelectorSharesCanonicalOrdering:
    """§3：snapshot flatten 与 history 共用同一 selector 语义。"""

    def test_flatten_latest_event_uses_bar_index_not_list_order(self) -> None:
        from app.services.first_pyramid_flatten import _latest_event_by_type

        # 已扁平化事件（barIndex 已写定），故意乱序
        events = [
            {"type": "OB_MITIGATED", "barIndex": 495, "direction": -1},
            {"type": "OB_CREATED", "barIndex": 300, "direction": 1},
        ]
        got = _latest_event_by_type(events, {"OB_CREATED", "OB_MITIGATED"})
        assert got is not None
        assert got["barIndex"] == 495

    def test_flatten_falls_back_to_list_order_when_bar_index_absent(self) -> None:
        from app.services.first_pyramid_flatten import _latest_event_by_type

        events = [
            {"type": "OB_CREATED", "direction": 1},
            {"type": "OB_CREATED", "direction": -1},
        ]
        got = _latest_event_by_type(events, {"OB_CREATED"})
        assert got is not None
        assert got["direction"] == -1


@pytest.mark.parametrize(
    "evt_type,keys",
    [
        ("OB_CREATED", ("confirmed_index", "confirmed_time")),
        ("OB_ENTERED", ("enter_index", "enter_time")),
        ("OB_MITIGATED", ("mitigated_index", "mitigated_time")),
    ],
)
def test_canonical_field_mapping_is_exhaustive(evt_type: str, keys: tuple[str, str]) -> None:
    evt = {"type": evt_type, keys[0]: 42, keys[1]: "2026-01-01"}
    assert ob_event_bar_index(evt) == 42
    assert ob_event_time(evt) == "2026-01-01"
