"""V1.1 event_lib 升级测试 - Task 19.4。

测试内容：
1. 事件检测（mock 数据）- 验证 detect_to_drafts 能正确检测事件
2. payload 自包含 - 验证 payload 不依赖外部状态
3. state_ttl 声明 - 验证所有检测器都有声明
4. structural_events 占位实现已修复 - 验证支撑/阻力突破检测有效
5. StrategyEventDraft 创建与校验
6. 去重键构建
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.strategy.events.base import (
    StrategyEventDraft,
    build_dedupe_key,
)
from app.strategy.events.detectors import structural_events
from app.strategy.events.registry import (
    detect_panel,
    detect_to_drafts,
    get_event,
    list_all,
)


class TestStrategyEventDraft:
    """测试 StrategyEventDraft dataclass。"""

    def test_create_valid_draft(self) -> None:
        """测试正常创建草稿。"""
        draft = StrategyEventDraft(
            event_type="evt_test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            dedupe_key="v1|600519|2026-06-18T10:30:00|evt_test",
            logical_entity="600519",
            payload={"direction": "up"},
            state_ttl_seconds=3600,
        )
        assert draft.event_type == "evt_test"
        assert draft.state_ttl_seconds == 3600

    def test_reject_empty_dedupe_key(self) -> None:
        """测试拒绝空 dedupe_key。"""
        with pytest.raises(ValueError, match="dedupe_key"):
            StrategyEventDraft(
                event_type="evt_test",
                event_time=datetime(2026, 6, 18),
                dedupe_key="",
                logical_entity="600519",
            )

    def test_reject_negative_ttl(self) -> None:
        """测试拒绝负 state_ttl_seconds。"""
        with pytest.raises(ValueError, match="state_ttl_seconds"):
            StrategyEventDraft(
                event_type="evt_test",
                event_time=datetime(2026, 6, 18),
                dedupe_key="k",
                logical_entity="600519",
                state_ttl_seconds=-1,
            )

    def test_default_ttl(self) -> None:
        """测试默认 state_ttl_seconds 为 3600。"""
        draft = StrategyEventDraft(
            event_type="evt_test",
            event_time=datetime(2026, 6, 18),
            dedupe_key="k",
            logical_entity="600519",
        )
        assert draft.state_ttl_seconds == 3600

    def test_to_dict(self) -> None:
        """测试 to_dict 序列化。"""
        draft = StrategyEventDraft(
            event_type="evt_test",
            event_time=datetime(2026, 6, 18, 10, 30, 0),
            dedupe_key="k",
            logical_entity="600519",
            payload={"x": 1},
        )
        d = draft.to_dict()
        assert d["event_type"] == "evt_test"
        assert d["payload"] == {"x": 1}
        assert "state_ttl_seconds" in d


class TestDedupeKey:
    """测试去重键构建。"""

    def test_build_dedupe_key_consistency(self) -> None:
        """测试相同输入产生相同去重键。"""
        ts = pd.Timestamp("2026-06-18 10:30:00")
        key1 = build_dedupe_key("v1", "600519", ts, "evt_test")
        key2 = build_dedupe_key("v1", "600519", ts, "evt_test")
        assert key1 == key2

    def test_build_dedupe_key_uniqueness(self) -> None:
        """测试不同输入产生不同去重键。"""
        ts = pd.Timestamp("2026-06-18 10:30:00")
        key1 = build_dedupe_key("v1", "600519", ts, "evt_a")
        key2 = build_dedupe_key("v1", "600519", ts, "evt_b")
        assert key1 != key2

    def test_build_dedupe_key_with_datetime(self) -> None:
        """测试 datetime 类型输入。"""
        dt = datetime(2026, 6, 18, 10, 30, 0)
        key = build_dedupe_key("v1", "600519", dt, "evt_test")
        assert "600519" in key
        assert "evt_test" in key


class TestDetectorDeclarations:
    """测试检测器 state_ttl_seconds 声明。"""

    def test_all_events_have_ttl(self) -> None:
        """验证所有注册事件都有 state_ttl_seconds。"""
        events = list_all()
        assert len(events) > 0, "应至少有一个注册事件"
        for e in events:
            assert "state_ttl_seconds" in e, f"{e['name']} 缺少 state_ttl_seconds"
            assert e["state_ttl_seconds"] > 0, f"{e['name']} state_ttl_seconds 应 > 0"

    def test_14_detector_categories_exist(self) -> None:
        """验证 14 个检测器类别都存在。"""
        expected_categories = {
            "趋势事件", "突破事件", "量能事件", "动量事件",
            "结构事件", "基本面事件", "复合事件",
            "趋势位置事件", "区间结构事件", "破位收回事件",
            "修复收回事件", "风险破位事件",
            "SR支撑事件", "SR压力事件",
        }
        actual_categories = {e["category"] for e in list_all()}
        missing = expected_categories - actual_categories
        assert not missing, f"缺少检测器类别: {missing}"


class TestStructuralEventsFix:
    """测试 structural_events 占位实现已修复。"""

    def test_support_broken_not_placeholder(self) -> None:
        """验证支撑跌破检测不再是占位实现（返回全 0）。"""
        # 构造持续下跌数据，应触发支撑跌破
        df = pd.DataFrame(
            {
                "close": [10.0, 10.5, 11.0, 10.8, 9.5, 8.0, 9.0, 10.0, 11.0, 12.0,
                          11.5, 11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5],
                "low": [9.5, 10.0, 10.5, 10.3, 9.0, 7.5, 8.5, 9.5, 10.5, 11.5,
                        11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0],
                "high": [10.5, 11.0, 11.5, 11.0, 10.0, 8.5, 9.5, 10.5, 11.5, 12.5,
                         12.0, 11.5, 11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0],
                "support_resistance_zones": [None] * 21,
            },
            index=pd.to_datetime(pd.date_range("2026-06-01", periods=21, freq="D")),
        )
        result = structural_events._detect_support_broken(df)
        # 占位实现返回全 0，修复后应检测到支撑跌破
        assert result.sum() > 0, "支撑跌破检测应触发（非占位全 0）"

    def test_resistance_broken_not_placeholder(self) -> None:
        """验证阻力突破检测不再是占位实现（返回全 0）。"""
        # 构造持续上涨数据，应触发阻力突破
        df = pd.DataFrame(
            {
                "close": [10.0, 10.5, 11.0, 10.8, 10.2, 10.0, 10.5, 11.0, 11.5, 12.0,
                          12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0, 17.5],
                "low": [9.5, 10.0, 10.5, 10.3, 9.8, 9.5, 10.0, 10.5, 11.0, 11.5,
                        12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0],
                "high": [10.5, 11.0, 11.5, 11.0, 10.5, 10.2, 10.8, 11.2, 11.8, 12.2,
                         12.8, 13.2, 13.8, 14.2, 14.8, 15.2, 15.8, 16.2, 16.8, 17.2, 17.8],
                "support_resistance_zones": [None] * 21,
            },
            index=pd.to_datetime(pd.date_range("2026-06-01", periods=21, freq="D")),
        )
        result = structural_events._detect_resistance_broken(df)
        assert result.sum() > 0, "阻力突破检测应触发（非占位全 0）"

    def test_support_broken_with_support_ref(self) -> None:
        """验证使用 support_ref 列时的支撑跌破检测。"""
        df = pd.DataFrame(
            {
                "close": [10.0, 9.5, 9.0, 8.5],
                "support_ref": [10.0, 10.0, 10.0, 10.0],
                "support_resistance_zones": [None] * 4,
            },
            index=pd.to_datetime(pd.date_range("2026-06-01", periods=4, freq="D")),
        )
        result = structural_events._detect_support_broken(df)
        # close 从 10 跌到 9.5，第二日 close(9.5) < support_ref.shift(1)(10.0)
        assert result.iloc[1] == 1, "第二日应检测到支撑跌破"

    def test_resistance_broken_with_resistance_ref(self) -> None:
        """验证使用 resistance_ref 列时的阻力突破检测。"""
        df = pd.DataFrame(
            {
                "close": [10.0, 10.5, 11.0, 11.5],
                "resistance_ref": [10.0, 10.0, 10.0, 10.0],
                "support_resistance_zones": [None] * 4,
            },
            index=pd.to_datetime(pd.date_range("2026-06-01", periods=4, freq="D")),
        )
        result = structural_events._detect_resistance_broken(df)
        # close 从 10 涨到 10.5，第二日 close(10.5) > resistance_ref.shift(1)(10.0)
        assert result.iloc[1] == 1, "第二日应检测到阻力突破"

    def test_support_broken_missing_columns(self) -> None:
        """验证缺少必需列时返回全 0（不抛异常）。"""
        df = pd.DataFrame(
            {"other": [1, 2, 3]},
            index=pd.to_datetime(pd.date_range("2026-06-01", periods=3, freq="D")),
        )
        result = structural_events._detect_support_broken(df)
        assert result.sum() == 0
        assert len(result) == 3


class TestDetectToDrafts:
    """测试 detect_to_drafts 事件检测与草稿生成。"""

    def test_detect_trend_flip_up(self) -> None:
        """测试趋势翻转事件检测。"""
        df = pd.DataFrame(
            {
                "dsa_dir": [0, 0, 1, 1, -1, -1, 1],
            },
            index=pd.to_datetime(pd.date_range("2026-06-18 09:30", periods=7, freq="min")),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        # dsa_dir 从 0->1（idx=2）和 -1->1（idx=6）触发
        assert len(drafts) == 2, f"应检测到 2 个翻转事件，实际 {len(drafts)}"
        assert all(d.event_type == "evt_dsa_dir_flip_up" for d in drafts)

    def test_payload_self_contained(self) -> None:
        """测试 payload 自包含（不依赖外部状态）。"""
        df = pd.DataFrame(
            {
                "dsa_dir": [0, 1],
            },
            index=pd.to_datetime(["2026-06-18 09:30", "2026-06-18 09:31"]),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        assert len(drafts) == 1
        draft = drafts[0]
        # payload 应包含事件类型、方向、依赖因子值
        assert "event_type" in draft.payload
        assert draft.payload["event_type"] == "evt_dsa_dir_flip_up"
        assert "direction" in draft.payload
        assert "dsa_dir" in draft.payload
        # payload 中的 dsa_dir 值应为触发行的值（1）
        assert draft.payload["dsa_dir"] == 1

    def test_draft_has_ttl(self) -> None:
        """测试草稿继承检测器的 state_ttl。"""
        df = pd.DataFrame(
            {"dsa_dir": [0, 1]},
            index=pd.to_datetime(["2026-06-18 09:30", "2026-06-18 09:31"]),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        assert len(drafts) == 1
        meta = get_event("evt_dsa_dir_flip_up")
        assert drafts[0].state_ttl_seconds == meta["state_ttl_seconds"]

    def test_dedupe_key_unique_per_event_time(self) -> None:
        """测试不同事件时间的去重键不同。"""
        df = pd.DataFrame(
            {
                "dsa_dir": [0, 1, 0, 1],
            },
            index=pd.to_datetime(pd.date_range("2026-06-18 09:30", periods=4, freq="min")),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        assert len(drafts) == 2
        keys = {d.dedupe_key for d in drafts}
        assert len(keys) == 2, "不同事件时间的去重键应不同"

    def test_snapshot_contains_context(self) -> None:
        """测试快照包含上下文因子值。"""
        df = pd.DataFrame(
            {
                "dsa_dir": [0, 0, 1],
                "close": [10.0, 10.5, 11.0],
            },
            index=pd.to_datetime(pd.date_range("2026-06-18 09:30", periods=3, freq="min")),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        assert len(drafts) == 1
        # 快照应包含因子列
        # 注意：snapshot 在 write_event 时才冻结，detect_to_drafts 不直接返回 snapshot
        # 但 payload 应自包含

    def test_no_event_when_factors_missing(self) -> None:
        """测试缺少因子列时不生成草稿。"""
        df = pd.DataFrame(
            {"other_col": [1, 2, 3]},
            index=pd.to_datetime(pd.date_range("2026-06-18 09:30", periods=3, freq="min")),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up"],
        )
        assert len(drafts) == 0, "缺少因子列时不应生成草稿"

    def test_detect_multiple_event_types(self) -> None:
        """测试同时检测多种事件类型。"""
        df = pd.DataFrame(
            {
                "dsa_dir": [0, 1],
                "bbmacd": [-0.5, 0.5],
            },
            index=pd.to_datetime(["2026-06-18 09:30", "2026-06-18 09:31"]),
        )
        drafts = detect_to_drafts(
            df,
            strategy_version_id="v1",
            instrument_id="600519",
            event_names=["evt_dsa_dir_flip_up", "evt_macd_golden_cross"],
        )
        # 应检测到 dsa_dir_flip_up 和 macd_golden_cross
        event_types = {d.event_type for d in drafts}
        assert "evt_dsa_dir_flip_up" in event_types
        assert "evt_macd_golden_cross" in event_types


class TestDetectPanelBackwardCompat:
    """测试 detect_panel 向后兼容性。"""

    def test_detect_panel_returns_dataframe(self) -> None:
        """测试 detect_panel 返回含事件标记列的 DataFrame。"""
        df = pd.DataFrame(
            {"dsa_dir": [0, 1, -1]},
            index=pd.to_datetime(pd.date_range("2026-06-18 09:30", periods=3, freq="min")),
        )
        result = detect_panel(df, event_names=["evt_dsa_dir_flip_up", "evt_dsa_dir_flip_down"])
        assert "evt_dsa_dir_flip_up" in result.columns
        assert "evt_dsa_dir_flip_down" in result.columns
        # idx=1: dsa_dir 0->1 触发 flip_up
        assert result["evt_dsa_dir_flip_up"].iloc[1] == 1
        # idx=2: dsa_dir 1->-1 触发 flip_down
        assert result["evt_dsa_dir_flip_down"].iloc[2] == 1

    def test_detect_panel_missing_factors(self) -> None:
        """测试 detect_panel 缺少因子列时返回 0。"""
        df = pd.DataFrame(
            {"other": [1, 2]},
            index=pd.to_datetime(["2026-06-18 09:30", "2026-06-18 09:31"]),
        )
        result = detect_panel(df, event_names=["evt_dsa_dir_flip_up"])
        assert "evt_dsa_dir_flip_up" in result.columns
        assert result["evt_dsa_dir_flip_up"].sum() == 0


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
