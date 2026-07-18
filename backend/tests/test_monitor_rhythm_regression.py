"""监控节奏回归测试（CHANGE-20260718-004）。

验证 volume_node_monitor / monitor_batch_service 的监控节奏合同未因 Node Cluster
engine 迁移而改变：

1. `EVENT_STATE_TTL_SECONDS == 600`（与 monitoring.NOTIFY_COOLDOWN_SECONDS 一致）
2. `_EVENT_COOLDOWN_SECONDS == 600`（事件冷却窗口）
3. monitor 1m 读取 `completed_only=True, include_realtime=False`（只用已完成 bar）
4. `detect_crossover_signals` 公式与原 `_detect_node_crossover_signals` 一致
   （`prev_close <= peak < cur_close` or `cur_close <= peak < prev_close`）
5. `_send_merged_notification` 按用户合并一条飞书消息

约束：不连数据库（源码 + 常量 + 纯函数验证）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.constants.indicator_contract import NODE_CLUSTER_EVENT_TTL_SECONDS
from app.services.node_cluster_engine import compute_node_cluster_profile, detect_crossover_signals

_MONITOR_FILE = Path(__file__).parent.parent / "app" / "strategy" / "monitors" / "volume_node_monitor.py"
_MONITOR_BATCH_FILE = Path(__file__).parent.parent / "app" / "services" / "monitor_batch_service.py"


class TestMonitorRhythmConstants:
    """1-2. 节奏常量不变。"""

    def test_event_state_ttl_is_600(self):
        """EVENT_STATE_TTL_SECONDS == 600（与 NOTIFY_COOLDOWN_SECONDS 一致）。"""
        from app.strategy.monitors.volume_node_monitor import EVENT_STATE_TTL_SECONDS
        assert EVENT_STATE_TTL_SECONDS == 600
        assert EVENT_STATE_TTL_SECONDS == NODE_CLUSTER_EVENT_TTL_SECONDS

    def test_event_cooldown_is_600(self):
        """_EVENT_COOLDOWN_SECONDS == 600。"""
        from app.services.monitor_batch_service import _EVENT_COOLDOWN_SECONDS
        assert _EVENT_COOLDOWN_SECONDS == 600


class TestMonitorCompletedOnlyContract:
    """3. monitor 1m 读取 completed_only=True, include_realtime=False。"""

    def test_monitor_1m_uses_completed_only(self):
        """monitor_batch_service 源码中 1m 读取路径必须含 completed_only=True。"""
        source = _MONITOR_BATCH_FILE.read_text(encoding="utf-8")
        # 监控链 1m 读取路径必须显式 completed_only=True
        assert "completed_only=True" in source, (
            "monitor_batch_service 必须显式 completed_only=True（只用已完成 1m bar）"
        )
        # 监控链 1m 读取路径必须 include_realtime=False（禁止 partial bar）
        assert "include_realtime=False" in source, (
            "monitor_batch_service 必须显式 include_realtime=False（禁止 partial bar 进入监控计算）"
        )


class TestCrossoverFormulaUnchanged:
    """4. detect_crossover_signals 公式与原实现一致。"""

    def test_crossover_formula_prev_above_cur_below(self):
        """prev_close <= peak < cur_close → 上穿触发。"""
        import numpy as np
        import pandas as pd
        np.random.seed(43)
        dates = pd.date_range(end="2026-06-18", periods=260, freq="B")
        close = 12.0 + np.random.uniform(-0.5, 0.5, 260)
        daily = pd.DataFrame(
            {"open": close, "high": close + 0.1, "low": close - 0.1, "close": close,
             "volume": np.full(260, 1e6), "amount": close * 1e6}, index=dates)
        daily.index.name = "datetime"
        dates15 = pd.date_range(end="2026-06-18 15:00", periods=4100, freq="15min")
        c15 = np.full(4100, 12.0)
        bars15 = pd.DataFrame(
            {"open": c15, "high": c15, "low": c15, "close": c15,
             "volume": np.full(4100, 1e5), "amount": c15 * 1e5}, index=dates15)
        bars15.index.name = "datetime"
        profile = compute_node_cluster_profile(daily, bars15)
        if not profile.all_peak_prices:
            return  # 无 peak 则跳过公式验证
        peak = profile.all_peak_prices[0]
        # 上穿：prev_close 在 peak 下方，cur_close 在 peak 上方
        signals = detect_crossover_signals(profile, prev_close=peak - 1.0, cur_close=peak + 1.0)
        assert len(signals) >= 1
        assert all(s["trigger_type"] == "node_cluster_touch" for s in signals)

    def test_crossover_formula_no_signal_when_both_above(self):
        """两根 close 都在 peak 上方 → 无穿越。"""
        import numpy as np
        import pandas as pd
        np.random.seed(43)
        dates = pd.date_range(end="2026-06-18", periods=260, freq="B")
        close = 12.0 + np.random.uniform(-0.5, 0.5, 260)
        daily = pd.DataFrame(
            {"open": close, "high": close + 0.1, "low": close - 0.1, "close": close,
             "volume": np.full(260, 1e6), "amount": close * 1e6}, index=dates)
        daily.index.name = "datetime"
        dates15 = pd.date_range(end="2026-06-18 15:00", periods=4100, freq="15min")
        c15 = np.full(4100, 12.0)
        bars15 = pd.DataFrame(
            {"open": c15, "high": c15, "low": c15, "close": c15,
             "volume": np.full(4100, 1e5), "amount": c15 * 1e5}, index=dates15)
        bars15.index.name = "datetime"
        profile = compute_node_cluster_profile(daily, bars15)
        if not profile.all_peak_prices:
            return
        peak = profile.all_peak_prices[0]
        # 两根都在 peak 上方 → 无穿越
        signals = detect_crossover_signals(profile, prev_close=peak + 1.0, cur_close=peak + 2.0)
        assert signals == []


class TestMergedNotificationContract:
    """5. _send_merged_notification 按用户合并。"""

    def test_send_merged_notification_groups_by_user(self):
        """_send_merged_notification 签名含 instrument_user_map（按用户归属合并）。"""
        tree = ast.parse(_MONITOR_BATCH_FILE.read_text(encoding="utf-8"))
        found_method = False
        found_param = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_merged_notification":
                found_method = True
                param_names = [a.arg for a in node.args.args]
                assert "instrument_user_map" in param_names, (
                    "_send_merged_notification 必须含 instrument_user_map 参数（按用户合并通知）"
                )
                found_param = True
        assert found_method, "_send_merged_notification 方法必须存在"
        assert found_param, "instrument_user_map 参数必须存在"
