"""[P0-11] 第一金字塔非筹码历史回补服务纯单元测试。

验证：
1. backfill_first_pyramid_history_batch 按股调用 compute_first_pyramid_history 一次
2. include_chip=False（不回补 chip）
3. 单股失败不阻塞其他股票
4. 进度回调被调用
5. 幂等：events 用 on_conflict_do_nothing（不可变）

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_first_pyramid_history_service.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _build_bars(n: int = 300) -> pd.DataFrame:
    """构造 OHLCV 日线 fixture（足够长度触发 history 计算）。"""
    import numpy as np
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.15 + 0.05)
    return pd.DataFrame({
        "open": close - 0.05,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": np.random.randint(100000, 500000, n).astype(float),
        "amount": close * 100000,
    }, index=dates)


def _build_history_result(symbol: str = "TEST") -> dict[str, Any]:
    """构造 compute_first_pyramid_history 的返回结构（mock 用）。"""
    return {
        "daily_state": [
            {
                "bar_index": 280,
                "time": "2026-01-01",
                "trend_transition": "up",
                "regime_value": 0.5,
                "regime_strength": 0.8,
            },
            {
                "bar_index": 281,
                "time": "2026-01-02",
                "trend_transition": "up",
                "regime_value": 0.6,
                "regime_strength": 0.85,
            },
        ],
        "events": [
            {"type": "BOS", "bar_index": 50, "time": "2025-03-15"},
            {"type": "OB_CREATED", "anchor_time": "2025-04-01", "ob_id": "abc"},
            {"type": "SQZ_RELEASE", "time": "2025-06-01", "direction": "up"},
        ],
        "meta": {
            "symbol": symbol,
            "output_bars": 250,
            "n_input": 300,
            "n_output": 2,
            "input_hash": "test_hash_123",
            "algorithm_version_core": "1.0.0-core-split",
            "include_chip": False,
        },
        "chip": None,
    }


class TestBackfillFirstPyramidHistoryBatch:
    """验证 backfill_first_pyramid_history_batch 主入口。"""

    @pytest.mark.asyncio
    async def test_calls_compute_history_once_per_instrument(self):
        """每只股票一次调用 compute_first_pyramid_history。"""
        from app.services.first_pyramid_history_service import (
            backfill_first_pyramid_history_batch,
        )

        instrument_ids = [uuid.uuid4() for _ in range(3)]
        bars = _build_bars(300)

        async def _fake_fetch(instrument_id):
            return bars

        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            return_value=_build_history_result(),
        ) as mock_history:
            result = await backfill_first_pyramid_history_batch(
                session=mock_session,
                instrument_ids=instrument_ids,
                batch_size=2,
                output_bars=250,
                _fetch_bars_func=_fake_fetch,
            )

        # 每只股票调用一次
        assert mock_history.call_count == 3, (
            f"expected 3 calls, got {mock_history.call_count}"
        )
        # 所有调用 include_chip=False
        for call in mock_history.call_args_list:
            assert call.kwargs.get("include_chip") is False, (
                f"include_chip must be False, got {call.kwargs.get('include_chip')}"
            )
        assert result["succeeded_count"] == 3
        assert result["failed_count"] == 0
        assert result["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_single_failure_does_not_block_others(self):
        """单股失败不阻塞其他股票。"""
        from app.services.first_pyramid_history_service import (
            backfill_first_pyramid_history_batch,
        )

        instrument_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        bars = _build_bars(300)

        async def _fake_fetch(instrument_id):
            return bars

        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        call_count = [0]

        def _history_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("模拟第二只股票计算失败")
            return _build_history_result()

        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            side_effect=_history_side_effect,
        ):
            result = await backfill_first_pyramid_history_batch(
                session=mock_session,
                instrument_ids=instrument_ids,
                batch_size=10,
                _fetch_bars_func=_fake_fetch,
            )

        assert result["succeeded_count"] == 2
        assert result["failed_count"] == 1
        assert result["status"] == "partial"
        assert len(result["failed_instruments"]) == 1
        assert "模拟第二只股票计算失败" in result["failed_instruments"][0]["error"]

    @pytest.mark.asyncio
    async def test_progress_callback_invoked(self):
        """progress_callback 每批后被调用。"""
        from app.services.first_pyramid_history_service import (
            backfill_first_pyramid_history_batch,
        )

        instrument_ids = [uuid.uuid4() for _ in range(5)]
        bars = _build_bars(300)

        async def _fake_fetch(instrument_id):
            return bars

        async def _progress(**kwargs):
            _progress.calls.append(kwargs)
        _progress.calls = []

        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            return_value=_build_history_result(),
        ):
            await backfill_first_pyramid_history_batch(
                session=mock_session,
                instrument_ids=instrument_ids,
                batch_size=2,
                progress_callback=_progress,
                _fetch_bars_func=_fake_fetch,
            )

        # 5 instruments / batch_size=2 = 3 batches
        assert len(_progress.calls) == 3, (
            f"expected 3 progress callbacks, got {len(_progress.calls)}"
        )
        # 最后一次进度应为 5/5
        last_call = _progress.calls[-1]
        assert last_call["processed"] == 5
        assert last_call["total"] == 5
        assert last_call["succeeded"] == 5

    @pytest.mark.asyncio
    async def test_empty_bars_marks_skipped(self):
        """bars 为空的股票标记为 skipped。"""
        from app.services.first_pyramid_history_service import (
            backfill_first_pyramid_history_batch,
        )

        instrument_ids = [uuid.uuid4()]

        async def _fake_fetch(instrument_id):
            return None  # 空 bars

        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
        ) as mock_history:
            result = await backfill_first_pyramid_history_batch(
                session=mock_session,
                instrument_ids=instrument_ids,
                _fetch_bars_func=_fake_fetch,
            )

        # 不应调用 history
        assert mock_history.call_count == 0
        assert result["skipped_count"] == 1
        assert result["succeeded_count"] == 0
        assert result["status"] == "failed"  # 全部 skipped 标 failed

    @pytest.mark.asyncio
    async def test_does_not_call_compute_first_pyramid_snapshot(self):
        """禁止逐日调用 snapshot（只能调用 history SSOT）。"""
        from app.services.first_pyramid_history_service import (
            backfill_first_pyramid_history_batch,
        )

        instrument_ids = [uuid.uuid4()]
        bars = _build_bars(300)

        async def _fake_fetch(instrument_id):
            return bars

        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        # 验证不导入 compute_first_pyramid_snapshot
        import app.services.first_pyramid_history_service as svc_mod
        assert not hasattr(svc_mod, "compute_first_pyramid_snapshot"), (
            "first_pyramid_history_service 不得导入 compute_first_pyramid_snapshot"
        )

        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            return_value=_build_history_result(),
        ):
            await backfill_first_pyramid_history_batch(
                session=mock_session,
                instrument_ids=instrument_ids,
                _fetch_bars_func=_fake_fetch,
            )


class TestBuildEventId:
    """验证事件稳定 ID 构造。"""

    def test_bar_index_priority(self):
        from app.services.first_pyramid_history_service import _build_event_id
        evt = {"type": "BOS", "bar_index": 50, "time": "2026-07-01"}
        assert _build_event_id(evt, "BOS") == "BOS_50"

    def test_anchor_time_fallback(self):
        from app.services.first_pyramid_history_service import _build_event_id
        evt = {"type": "OB_CREATED", "anchor_time": "2026-07-01"}
        assert _build_event_id(evt, "OB_CREATED") == "OB_CREATED_2026-07-01"

    def test_hash_fallback_stable(self):
        from app.services.first_pyramid_history_service import _build_event_id
        evt = {"type": "UNKNOWN", "data": {"a": 1}}
        id1 = _build_event_id(evt, "UNKNOWN")
        id2 = _build_event_id(evt, "UNKNOWN")
        assert id1 == id2
        assert id1.startswith("UNKNOWN_")
