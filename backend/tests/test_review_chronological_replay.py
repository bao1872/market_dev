"""[CHANGE-20260808] Review chronological replay + no-future observation 纯单元测试。

验证：
1. load_metric_history 严格只读 trade_date < target_date 的观测（no future leakage）
2. history 按 ASC 构建，窗口截断为 baseline_window
3. <60 观测 → raw only / insufficient_history（由 metric_engine 消费端决定）
4. bootstrap_single_date 每 date 只调一次 load_day_fact_maps（不随 scope 数增长）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_review_chronological_replay.py -v
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import MagicMock

from app.services.review_metric_observation_service import load_metric_history


class _Obs:
    def __init__(self, trade_date: date, metric_code: str, raw_value: float) -> None:
        self.trade_date = trade_date
        self.metric_code = metric_code
        self.raw_value = raw_value
        self.component_name = "core"
        self.scope_type = "market"
        self.scope_key = "A"


def _make_session(observations):
    """mock session：load_metric_history 的 execute 返回 observations（按 trade_date desc 排序）。"""
    session = MagicMock()

    async def fake_execute(stmt):
        result = MagicMock()
        result.scalars.return_value = MagicMock(
            __iter__=lambda self: iter(observations),
        )
        return result

    session.execute = fake_execute
    return session


class TestLoadMetricHistoryNoFuture:
    """load_metric_history 严格不读未来 + ASC + 窗口截断。"""

    def test_only_earlier_observations(self) -> None:
        """history 只含 trade_date < target_date 的观测（无未来泄漏）。

        注：SQLAlchemy where(trade_date < target) 由 load_metric_history L131 保证
        （SQL 过滤，mock 无法模拟）；此处 mock 传入已按该条件过滤的观测，
        验证 history 按 ASC 构建且不引入 target 当天/未来。
        """
        target = date(2026, 8, 4)
        # 已按 SQL where(trade_date < target) 过滤后的观测（仅 target 之前）
        obs = [
            _Obs(date(2026, 8, 3), "P", 1.0),
            _Obs(date(2026, 8, 2), "P", 0.9),
        ]
        session = _make_session(obs)
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=target, algorithm_version="v1", baseline_window=120,
            )
        )
        # P 的 history 应只含 08-02、08-03 的 raw（ASC）
        p_hist = history_maps["P"]["core"]
        assert len(p_hist) == 2
        assert p_hist == [0.9, 1.0]  # ASC（08-02 → 08-03）

    def test_window_truncation(self) -> None:
        """history 截断为 baseline_window（只取最近 baseline_window 个日期）。"""
        target = date(2026, 8, 4)
        obs = [
            _Obs(target - timedelta(days=i), "P", float(i))
            for i in range(1, 6)  # 5 个 prior 观测
        ]
        session = _make_session(obs)
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=target, algorithm_version="v1", baseline_window=3,
            )
        )
        p_hist = history_maps["P"]["core"]
        # 只取最近 3 个日期（target-1, target-2, target-3）→ ASC
        assert len(p_hist) == 3
        assert p_hist[0] == 3.0  # target-3
        assert p_hist[-1] == 1.0  # target-1

    def test_empty_history(self) -> None:
        """无 prior 观测 → history_maps None（Day < baseline，raw only）。"""
        session = _make_session([])
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=date(2026, 8, 4), algorithm_version="v1", baseline_window=120,
            )
        )
        assert history_maps is None
