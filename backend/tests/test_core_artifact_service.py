"""core_artifact_service.compute_core_artifact 单元测试（P0-03/P0-05）。

[CHANGE-20260805-CP4A]
验证：
- 每股 DSA/SMC/momentum/canonical_frame 各只计算一次（compute-once 计数）。
- First Pyramid 由纯 builder 组装，本函数不重复调用算法 kernel。
- dsa_vwap / regime / anchor 从 raw dsa_bundle 提取（round-trip，不解析中文摘要）。
- lineage（parameter_hash / source_core_run_id / algorithm_versions）注入。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.core_artifact_service import compute_core_artifact
from app.services.core_run_context import CoreRunContext


def _synthetic_daily(n: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    close = 10 + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": rng.integers(100000, 500000, n).astype(float),
            "amount": close * 300000,
        },
        index=idx,
    )


def _context(run_id: str = "run-1") -> CoreRunContext:
    return CoreRunContext(
        trade_date=pd.Timestamp("2026-07-01").date(),
        run_calculated_at=pd.Timestamp("2026-07-01T15:00:00").to_pydatetime(),
        algorithm_versions={
            "dsa": "dsa-v1",
            "smc": "smc-v1",
            "momentum": "momentum-v1",
        },
        config={"dsa": {"min_dir_bars": 50}},
        run_id=run_id,
    )


def test_compute_core_artifact_once_per_algorithm() -> None:
    """每股各算法只调用一次（compute-once 计数，P0-03）。"""
    ctx = _context()
    artifact = compute_core_artifact(
        context=ctx,
        instrument_id="i1",
        symbol="000001",
        daily_frame=_synthetic_daily(),
        input_hash="in-1",
        bars_hash="bars-1",
        adj_factor_hash="adj-1",
    )
    counts = artifact.diagnostics
    assert counts["canonical_frame_build"] == 1
    assert counts["dsa"] == 1
    assert counts["smc"] == 1
    assert counts["momentum"] == 1


def test_compute_core_artifact_two_calls_accumulate() -> None:
    """同一 run 两次调用 → 计数累积为 2（用于 eligible_count 对账）。"""
    ctx = _context()
    for _ in range(2):
        compute_core_artifact(
            context=ctx,
            instrument_id="i1",
            symbol="000001",
            daily_frame=_synthetic_daily(),
            input_hash="in",
            bars_hash="bars",
            adj_factor_hash="adj",
        )
    assert ctx.compute_diagnostics.to_dict()["dsa"] == 2


def test_compute_core_artifact_first_pyramid_built() -> None:
    """First Pyramid core 由纯 builder 组装（不重新调用 kernel）。"""
    ctx = _context()
    artifact = compute_core_artifact(
        context=ctx,
        instrument_id="i1",
        symbol="000001",
        daily_frame=_synthetic_daily(),
        input_hash="in",
        bars_hash="bars",
        adj_factor_hash="adj",
    )
    fp = artifact.payload["first_pyramid"]
    assert fp["nBars"] == 120
    assert fp["symbol"] == "000001"
    # 三维（trend/structure/momentum）均存在
    for dim in ("trend", "structure", "momentum"):
        assert dim in fp


def test_compute_core_artifact_dsa_vwap_roundtrip() -> None:
    """dsa_vwap 从 raw dsa_bundle 提取到 metrics 与 visual（P0-05）。"""
    ctx = _context()
    artifact = compute_core_artifact(
        context=ctx,
        instrument_id="i1",
        symbol="000001",
        daily_frame=_synthetic_daily(),
        input_hash="in",
        bars_hash="bars",
        adj_factor_hash="adj",
    )
    dsa_metrics = artifact.payload.get("dsa", {})
    assert "dsa_vwap" in dsa_metrics
    assert dsa_metrics["dsa_vwap"] > 0
    assert artifact.visual.get("dsa_vwap") == dsa_metrics["dsa_vwap"]


def test_compute_core_artifact_lineage() -> None:
    """parameter_hash / source_core_run_id / algorithm_versions 注入。"""
    ctx = _context()
    artifact = compute_core_artifact(
        context=ctx,
        instrument_id="i1",
        symbol="000001",
        daily_frame=_synthetic_daily(),
        input_hash="in",
        bars_hash="bars",
        adj_factor_hash="adj",
    )
    assert artifact.parameter_hash
    assert artifact.source_core_run_id == "run-1"
    assert artifact.algorithm_versions["dsa"] == "dsa-v1"


def test_compute_core_artifact_empty_frame_raises() -> None:
    """空 daily_frame → ValueError。"""
    ctx = _context()
    with pytest.raises(ValueError):
        compute_core_artifact(
            context=ctx,
            instrument_id="i1",
            symbol="000001",
            daily_frame=pd.DataFrame(),
            input_hash="in",
            bars_hash="bars",
            adj_factor_hash="adj",
        )


# ============================================================================
# [CHANGE-20260805-CP4A / P0-03] compute-once spy：算法 kernel 每股只算一次
# ============================================================================


def test_compute_core_artifact_kernel_spy_once(monkeypatch) -> None:
    """无 precomputed 时，每股 DSA/SMC/Bollinger/SQZMOM kernel 恰好一次。"""
    from app.services import first_pyramid_service as fps

    calls = {"dsa": 0, "smc": 0, "bollinger": 0, "sqz": 0}

    real_dsa = fps.compute_dsa_bundle
    real_smc = fps.compute_smc_pine
    real_bb = fps.compute_bollinger_features
    real_sqz = fps.compute_sqzmom_lb

    def _spy_dsa(*a, **kw):
        calls["dsa"] += 1
        return real_dsa(*a, **kw)

    def _spy_smc(*a, **kw):
        calls["smc"] += 1
        return real_smc(*a, **kw)

    def _spy_bb(*a, **kw):
        calls["bollinger"] += 1
        return real_bb(*a, **kw)

    def _spy_sqz(*a, **kw):
        calls["sqz"] += 1
        return real_sqz(*a, **kw)

    monkeypatch.setattr(fps, "compute_dsa_bundle", _spy_dsa)
    monkeypatch.setattr(fps, "compute_smc_pine", _spy_smc)
    monkeypatch.setattr(fps, "compute_bollinger_features", _spy_bb)
    monkeypatch.setattr(fps, "compute_sqzmom_lb", _spy_sqz)

    compute_core_artifact(
        context=_context(),
        instrument_id="i1",
        symbol="000001",
        daily_frame=_synthetic_daily(),
        input_hash="in",
        bars_hash="bars",
        adj_factor_hash="adj",
    )

    assert calls == {"dsa": 1, "smc": 1, "bollinger": 1, "sqz": 1}


def test_compute_core_artifact_kernel_spy_reused(monkeypatch) -> None:
    """提供 precomputed_raw 时，kernel 不再重算（P0-03：与 structural 共享 raw）。"""
    from app.services import first_pyramid_service as fps
    from app.services.first_pyramid_service import _compute_first_pyramid_raw_results

    # 先无 spy 构建 raw（供后续传入 precomputed_raw）
    df = _synthetic_daily()
    raw = _compute_first_pyramid_raw_results(df)

    calls = {"dsa": 0, "smc": 0, "bollinger": 0, "sqz": 0}
    real_dsa = fps.compute_dsa_bundle
    real_smc = fps.compute_smc_pine
    real_bb = fps.compute_bollinger_features
    real_sqz = fps.compute_sqzmom_lb

    def _spy_dsa(*a, **kw):
        calls["dsa"] += 1
        return real_dsa(*a, **kw)

    def _spy_smc(*a, **kw):
        calls["smc"] += 1
        return real_smc(*a, **kw)

    def _spy_bb(*a, **kw):
        calls["bollinger"] += 1
        return real_bb(*a, **kw)

    def _spy_sqz(*a, **kw):
        calls["sqz"] += 1
        return real_sqz(*a, **kw)

    monkeypatch.setattr(fps, "compute_dsa_bundle", _spy_dsa)
    monkeypatch.setattr(fps, "compute_smc_pine", _spy_smc)
    monkeypatch.setattr(fps, "compute_bollinger_features", _spy_bb)
    monkeypatch.setattr(fps, "compute_sqzmom_lb", _spy_sqz)

    # 传入 precomputed_raw 后，compute_core_artifact 不得再调用任何 kernel
    compute_core_artifact(
        context=_context(),
        instrument_id="i1",
        symbol="000001",
        daily_frame=df,
        input_hash="in",
        bars_hash="bars",
        adj_factor_hash="adj",
        precomputed_raw=raw,
    )

    # kernel 0 次：全部复用 precomputed_raw（structural 与 pyramid 共享同一份 raw）
    assert calls == {"dsa": 0, "smc": 0, "bollinger": 0, "sqz": 0}
