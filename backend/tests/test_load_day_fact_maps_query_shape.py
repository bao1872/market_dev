"""[CHANGE-20260808] load_day_fact_maps query-shape 验证。

核心 contract：
- 同一 trade_date 只做固定次数批量查询（当日 FP / 前日 FP / bars / identity），
  不随 scope 数量或 instrument 数量线性增长。
- 禁止为每个 scope 重复读取 historical bars（400 日）。
- 返回 facts_by_instrument 供任意 scope 从内存筛选复用。

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_load_day_fact_maps_query_shape.py -v
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.services.review_scope_service import load_day_fact_maps


class _State:
    def __init__(self, instrument_id: uuid.UUID, trade_date: date, payload: dict) -> None:
        self.id = uuid.uuid4()
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.state_payload = payload
        self.input_hash = "hash-x"
        # [CHANGE-20260808] M2 lineage columns
        self.source_history_run_id = uuid.uuid4()
        self.history_contract_version = "review-history-v2"


class _Bar:
    def __init__(self, instrument_id, trade_date, close, prev_close, volume, amount) -> None:
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.open = prev_close
        self.high = max(close, prev_close)
        self.low = min(close, prev_close)
        self.close = close
        self.volume = volume
        self.amount = amount


class _Instrument:
    def __init__(self, instrument_id, symbol) -> None:
        self.id = instrument_id
        self.symbol = symbol
        self.name = symbol


def _make_session(
    instruments, current_payload, bar_sets,
    previous_payload=None, contract_version="review-history-v2",
    previous_contract_version=None,
):
    """构造 mock AsyncSession：按调用顺序返回 5 次 date-level 批量查询结果。"""
    session = MagicMock()
    call_count = {"n": 0}

    async def fake_execute(stmt):
        call_count["n"] += 1
        n = call_count["n"]
        fake_result = MagicMock()
        if n == 1:  # 当日 FP state
            _payload = {
                "history_contract_version": contract_version,
                **(current_payload or {}),
            }
            states = [
                _State(inst, date(2026, 8, 4), _payload)
                for inst in instruments
            ]
            fake_result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(states),
            )
        elif n == 2:  # 前日 FP state
            _prev_ver = previous_contract_version or contract_version
            _prev_payload = {
                "history_contract_version": _prev_ver,
                **(previous_payload or {"regime_value": 1}),
            }
            prev = [
                _State(inst, date(2026, 8, 3), _prev_payload)
                for inst in instruments
            ]
            fake_result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(prev),
            )
        elif n == 3:  # current BarDaily（trade_date == target_date）
            bars = [
                _Bar(inst, date(2026, 8, 4), c, prev_c, vol, amt)
                for inst, (c, prev_c, vol, amt)
                in zip(instruments, bar_sets, strict=False)
            ]
            fake_result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(bars),
            )
        elif n == 4:  # previous BarDaily（trade_date < target_date，每 instrument 最近 1 根）
            bars = [
                _Bar(inst, date(2026, 8, 3), prev_c, prev_c - 0.1, vol, amt)
                for inst, (c, prev_c, vol, amt)
                in zip(instruments, bar_sets, strict=False)
            ]
            fake_result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(bars),
            )
        else:  # identity
            idents = [
                _Instrument(inst, f"SYM{i}") for i, inst in enumerate(instruments)
            ]
            fake_result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(idents),
            )
        return fake_result

    session.execute = fake_execute
    return session, call_count


class TestLoadDayFactMapsQueryShape:
    """验证 load_day_fact_maps 的 query-shape 与 facts 构建。"""

    def test_fixed_query_count_regardless_instruments(self) -> None:
        """同一 trade_date 无论多少 instrument，都只做固定 5 次 date-level 批量查询。"""
        ids = [uuid.uuid4() for _ in range(3)]
        payload = {
            "regime_value": 1,
            "swing_bias": 1,
            "internal_bias": 1,
            "volume_ratio_20": 1.2,
            "volume_percentile_20": 70.0,
            "price_position_120d": 0.6,
            "latest_bos_direction": "up",
            "latest_bos_freshness": 2,
        }
        bar_sets = [(10.0, 9.8, 1000.0, 10000.0) for _ in range(3)]
        session, calls = _make_session(ids, payload, bar_sets)

        facts = asyncio.run(
            load_day_fact_maps(
                session, trade_date=date(2026, 8, 4), instrument_ids=ids,
            )
        )

        # 关键断言：无论 instrument 数多少，查询次数固定为 5（不随 scope/instrument 数增长）
        assert calls["n"] == 5
        assert len(facts) == 3

    def test_facts_have_review_fields(self) -> None:
        """facts 包含 review_return_1d / amount / rolling facts（不重复读 400 日）。"""
        ids = [uuid.uuid4()]
        payload = {
            "regime_value": 1,
            "swing_bias": 1,
            "internal_bias": 1,
            "volume_ratio_20": 1.5,
            "volume_percentile_20": 80.0,
            "price_position_120d": 0.7,
            "latest_bos_direction": "up",
            "latest_bos_freshness": 1,
        }
        # close=10.0, prev=9.5 → return_1d = (10-9.5)/9.5*100 ≈ 5.26
        bar_sets = [(10.0, 9.5, 500.0, 5000.0)]
        session, _ = _make_session(ids, payload, bar_sets)

        facts = asyncio.run(
            load_day_fact_maps(
                session, trade_date=date(2026, 8, 4), instrument_ids=ids,
            )
        )
        fact = facts[ids[0]]
        assert fact["review_return_1d"] is not None
        assert abs(fact["review_return_1d"] - ((10.0 - 9.5) / 9.5 * 100.0)) < 1e-6
        assert fact["review_amount"] == 5000.0
        assert fact["review_volume"] == 500.0
        assert fact["fp_trend_direction"] == "上行"
        assert fact["review_price_position"] == 0.7
        assert fact["fp_latest_bos_direction"] == "bullish"
        assert fact["fp_latest_bos_freshness"] == 1

    def test_mixed_contract_version_rejected(self) -> None:
        """旧 contract version 的 state 必须 fail closed（HISTORY_CONTRACT_VERSION_MISMATCH）。"""
        ids = [uuid.uuid4()]
        payload = {"regime_value": 1}
        bar_sets = [(10.0, 9.8, 1000.0, 10000.0)]
        # 旧 payload 无 history_contract_version 或版本不匹配
        session, _ = _make_session(
            ids, payload, bar_sets, contract_version="review-history-v1",
        )
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(
                load_day_fact_maps(
                    session, trade_date=date(2026, 8, 4), instrument_ids=ids,
                )
            )
        assert "HISTORY_CONTRACT_VERSION_MISMATCH" in str(exc_info.value)

    def test_mixed_previous_contract_version_rejected(self) -> None:
        """current=v2 + previous=v1 必须 fail closed（previous state contract guard）。"""
        ids = [uuid.uuid4()]
        payload = {"regime_value": 1}
        bar_sets = [(10.0, 9.8, 1000.0, 10000.0)]
        # current=v2，previous=v1（旧版本）
        session, _ = _make_session(
            ids, payload, bar_sets,
            contract_version="review-history-v2",
            previous_contract_version="review-history-v1",
        )
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(
                load_day_fact_maps(
                    session, trade_date=date(2026, 8, 4), instrument_ids=ids,
                )
            )
        assert "HISTORY_CONTRACT_VERSION_MISMATCH(previous)" in str(exc_info.value)
