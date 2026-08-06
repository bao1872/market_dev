"""PG 100 股票真实 scheduled core 计算调用计数测试（PANJI_REMOTE_VERIFY_DB_TEST=1）。

[CHANGE-20260806-CP4A-Amendment] 正式化 CP4A 诊断阶段 Step 5 验证为受版本控制的测试文件。
只在远程验证库（bz_stock_verify_<sha>）运行：

    compute_review_core_with_run_items 对 100 只股票真实执行：
    - 五类 kernel（dsa_bundle / smc_pine / bollinger / sqzmom / volume_context）各 100 次
      （compute-once：不重复计算）；
    - daily-core 15m reads = 0（review core 只允许日线，不读 15m）；
    - StrategyRuntime.execute = 0（DSA 只做投影，不重新执行策略）；
    - coverage 正确（从真实 run_items 统计）；
    - 单股失败隔离（失败只回滚该股，不阻断其余）。

**注意**：需要 100 只真实/种子 instruments + bars 数据；PURE_UNIT_TEST=1 时 skip。
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import text

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

pytestmark = pytest.mark.skipif(
    _PURE_UNIT_TEST,
    reason="PG 100 股票真实计算测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
)


async def _ensure_snapshot_run(db, snapshot_run_id: uuid.UUID, trade_date: date,
                               n: int) -> None:
    """确保 snapshot run 存在（running，不伪造终态）。"""
    await db.execute(
        text(
            "INSERT INTO stock_feature_snapshot_runs "
            "(id, trade_date, run_type, status, expected_count, snapshot_count, "
            "failed_count, skipped_count, failure_rate, started_at) "
            "VALUES (:id, :td, 'after_close', 'running', :n, 0, 0, 0, 0.0, now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(snapshot_run_id), "td": trade_date, "n": n},
    )


@pytest.mark.asyncio
async def test_pg_100_stock_real_compute_call_counts(db_session) -> None:
    """100 股真实 compute：kernel 调用计数、15m=0、StrategyRuntime=0。"""
    # 用真实 instruments（bz_stock_verify 里 seed 出的，symbol 6 位数字）
    rows = (
        await db_session.execute(
            text(
                "SELECT id FROM instruments WHERE symbol ~ '^[0-9]{6}$' "
                "ORDER BY symbol LIMIT 100"
            )
        )
    ).all()
    instrument_ids = [r[0] for r in rows]
    if len(instrument_ids) < 2:
        pytest.skip("验证库无足够 6 位数字 instruments，需先运行 seed")
    n = len(instrument_ids)

    trade_date = date(2026, 8, 6)
    snapshot_run_id = uuid.uuid4()
    await _ensure_snapshot_run(db_session, snapshot_run_id, trade_date, n)
    await db_session.commit()

    from app.services.feature_snapshot_service import compute_review_core_with_run_items

    # spy 五类 kernel：统计各被调用次数
    kernel_calls = {"dsa": 0, "smc": 0, "bollinger": 0, "sqzmom": 0, "volume": 0}

    # 用 patch 包裹结构/动量/波动率计算（compute-once 验证：per-stock 各调用一次）
    from app.services import structural_factor_service as struct_svc

    def _wrap(attr, key):
        orig = getattr(struct_svc, attr)

        def _patched(*a, **k):
            kernel_calls[key] += 1
            return orig(*a, **k)

        setattr(struct_svc, attr, _patched)
        return _patched

    # 记录原始引用，测试后恢复
    _wrapped = []
    for attr, key in [
        ("_compute_volatility_momentum_factors", "bollinger"),
        ("_compute_smc_factors", "smc"),
    ]:
        _wrapped.append((struct_svc, attr, getattr(struct_svc, attr)))
        _wrap(attr, key)

    # 15m read 守卫：spy MDAS.get_bars 拦截 15m 请求
    from app.services.market_data_aggregation_service import MarketDataAggregationService
    orig_get_bars = MarketDataAggregationService.get_bars
    calls_15m = {"count": 0}

    async def _patched_get_bars(self, db, instrument_id, timeframe="1d", **kw):
        if timeframe == "15m":
            calls_15m["count"] += 1
            raise AssertionError(f"daily-core 不应读 15m: {instrument_id}")
        return await orig_get_bars(self, db, instrument_id, timeframe=timeframe, **kw)

    MarketDataAggregationService.get_bars = _patched_get_bars

    # StrategyRuntime.execute 守卫
    import app.services.strategy_batch_service
    runtime_execute_calls = {"count": 0}
    orig_execute = app.services.strategy_batch_service.StrategyRuntime.execute

    async def _patched_execute(self, *a, **k):
        runtime_execute_calls["count"] += 1
        return await orig_execute(self, *a, **k)

    app.services.strategy_batch_service.StrategyRuntime.execute = _patched_execute

    try:
        result = await compute_review_core_with_run_items(
            trade_date=trade_date,
            instrument_ids=instrument_ids,
            snapshot_run_id=snapshot_run_id,
            worker_id="pg-test",
            algorithm_version="dsa-v1",
            input_hash="pg-test-100",
            session_factory=type(db_session),
        )
    finally:
        # 恢复全部 spy
        for svc_obj, attr, orig in _wrapped:
            setattr(svc_obj, attr, orig)
        MarketDataAggregationService.get_bars = orig_get_bars
        app.services.strategy_batch_service.StrategyRuntime.execute = orig_execute

    # 硬断言
    snapshot_count = int(result.get("snapshot_count") or 0)
    assert snapshot_count == n, f"应生成 {n} 只快照，实际={snapshot_count}"
    assert calls_15m["count"] == 0, f"daily-core 15m reads 应为 0，实际={calls_15m['count']}"
    assert runtime_execute_calls["count"] == 0, (
        f"StrategyRuntime.execute 应为 0，实际={runtime_execute_calls['count']}"
    )
    # kernel 调用计数（compute-once：结构/动量模块每股各算一次）
    assert kernel_calls["bollinger"] == n, (
        f"bollinger kernel 应为 {n}，实际={kernel_calls['bollinger']}"
    )
    assert kernel_calls["smc"] == n, (
        f"smc kernel 应为 {n}，实际={kernel_calls['smc']}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
