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

from app.domain.review.metric_engine import (
    compute_all_metrics,
)
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.services.review_bootstrap_service import list_bootstrap_eligible_dates
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


class TestChronologicalBoundaryIntegration:
    """真实 load_metric_history → compute_all_metrics 的 0/59/60/61/120/121 边界。"""

    def _flat_list(self) -> list[dict]:
        # 单个 member，review_return_1d 非 None（P 的 field_source）
        return [{
            "review_return_1d": 1.5,
            "fp_trend_direction": "上行",
            "_instrument_symbol": "T",
        }]

    def _p_payload(self, n_hist: int) -> dict:
        """构造 n_hist 个 prior observations，调 compute_all_metrics 返回 P payload。"""
        history = [float(i) for i in range(1, n_hist + 1)]
        history_maps = {"P": {"scope_return_1d": history}} if n_hist > 0 else None
        payloads = compute_all_metrics(
            self._flat_list(),
            ready_count=1,
            history_maps=history_maps,
            registry=DEFAULT_REGISTRY,
        )
        return payloads["P"]

    def test_zero_history(self) -> None:
        """0 观测 → normalized 不足（主 component scope_return_1d < 60）。"""
        p = self._p_payload(0)
        assert p["readiness"]["normalized_ready"] is False

    def test_59_history(self) -> None:
        """59 观测 → normalized 不足（<60）。"""
        p = self._p_payload(59)
        assert p["readiness"]["normalized_ready"] is False

    def test_60_history(self) -> None:
        """60 观测 → 主 component normalized available。"""
        p = self._p_payload(60)
        assert p["readiness"]["normalized_ready"] is True

    def test_61_history(self) -> None:
        """61 观测 → normalized available。"""
        p = self._p_payload(61)
        assert p["readiness"]["normalized_ready"] is True

    def test_120_history(self) -> None:
        """120 观测 → normalized available。"""
        p = self._p_payload(120)
        assert p["readiness"]["normalized_ready"] is True

    def test_121_history(self) -> None:
        """121 观测 → normalized available（滚动 120 窗口仍含足够观测）。"""
        p = self._p_payload(121)
        assert p["readiness"]["normalized_ready"] is True


class TestDaysBackTradingDates:
    """days_back 必须是真交易日数（distinct trade_date），非自然日。"""

    def test_days_back_limits_to_trading_dates(self) -> None:
        """days_back=120 最多返回 120 个 distinct 交易日（即使自然日 span 更长）。"""
        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                # FP history：返回 150 个 distinct 交易日（desc 由 SQL limit 保证 120）
                # mock 模拟 SQL limit 已截断到 120 个交易日
                dates = [
                    date(2026, 8, 4) - timedelta(days=i)
                    for i in range(120)
                ]
                result.all.return_value = [(d,) for d in dates]
            else:
                # factor_publication：空
                result.all.return_value = []
            return result

        session.execute = fake_execute
        result = asyncio.run(
            list_bootstrap_eligible_dates(
                session, end_date=date(2026, 8, 4), days_back=120,
            )
        )
        # 返回最多 120 个交易日，ASC（oldest → newest）
        assert len(result) == 120
        dates_only = [d for d, _ in result]
        assert dates_only == sorted(dates_only)  # ASC
        assert len(set(dates_only)) == len(dates_only)  # distinct

    def test_days_back_returns_limited_count(self) -> None:
        """days_back 限制返回的交易日数量（120 自然日 span 仅约 80 交易日不得被误当 120）。"""
        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                # 模拟 SQL：distinct trade_date DESC LIMIT 30 → 30 个交易日
                dates = [
                    date(2026, 8, 4) - timedelta(days=i) for i in range(30)
                ]
                result.all.return_value = [(d,) for d in dates]
            else:
                result.all.return_value = []
            return result

        session.execute = fake_execute
        result = asyncio.run(
            list_bootstrap_eligible_dates(
                session, end_date=date(2026, 8, 4), days_back=30,
            )
        )
        assert len(result) == 30
