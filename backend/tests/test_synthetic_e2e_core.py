"""纯行为 synthetic E2E：daily bars → CoreRunContext → artifact → codec → projection。

[CHANGE-20260805-CP4A-CP3 / Step 7]
不连 PG，纯本地可执行，验证核心链贯通：
    daily bars
    → resolve(冻结) CoreRunContext
    → compute_core_artifact（唯一 kernel owner，每股各算法一次）
    → encode_dsa_projection_to_summary（versioned）
    → decode（强类型）
    → map_dsa_projection（DSA projection 指标一致）
    → first_pyramid / state-event candidates / availability / lineage

PG synthetic E2E（snapshot persistence → atomic publication → StrategyRun）见
test_synthetic_e2e_pg.py（仅可收集，暂不执行）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.core_artifact_codec import (
    CORE_ARTIFACT_SCHEMA_VERSION,
    decode_dsa_projection_from_summary,
    encode_dsa_projection_to_summary,
)
from app.services.core_artifact_service import compute_core_artifact
from app.services.core_run_context import CoreRunContext
from app.services.dsa_projection_service import map_dsa_projection


def _daily_bars(n: int = 250) -> pd.DataFrame:
    idx = pd.date_range("2025-11-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    close = 20 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame(
        {
            "open": close - 0.08,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": rng.integers(200000, 900000, n).astype(float),
            "amount": close * 600000,
        },
        index=idx,
    )


def test_synthetic_e2e_core_chain() -> None:
    """全链贯通：context→artifact→codec→projection→first_pyramid→state events→availability。"""
    from app.services.core_artifact_service import compute_core_kernel_bundle

    daily = _daily_bars(250)
    ctx = CoreRunContext(
        trade_date=pd.Timestamp("2026-08-06").date(),
        run_calculated_at=pd.Timestamp("2026-08-06T15:00:00").to_pydatetime(),
        algorithm_versions={"dsa": "dsa-v3", "smc": "smc-v1", "momentum": "momentum-v1"},
        config={"dsa": {"min_dir_bars": 50}, "eligible_universe_hash": "u1"},
        run_id="core-run-e2e",
    )

    # 1. 唯一 owner 计算 raw bundle
    raw = compute_core_kernel_bundle(daily)

    # 2. artifact（复用 raw → kernel 不重复）
    artifact = compute_core_artifact(
        context=ctx,
        instrument_id="i-e2e",
        symbol="000001",
        daily_frame=daily,
        input_hash="in-e2e",
        bars_hash="bars-e2e",
        adj_factor_hash="adj-e2e",
        precomputed_raw=raw,
    )

    # 3. 可用性与 lineage
    assert artifact.availability["trend"] in ("ready", "unavailable")
    assert artifact.parameter_hash
    assert artifact.source_core_run_id == "core-run-e2e"
    assert artifact.algorithm_versions["dsa"] == "dsa-v3"

    # 4. First Pyramid 三维存在
    fp = artifact.payload["first_pyramid"]
    for dim in ("trend", "structure", "momentum"):
        assert dim in fp

    # 5. versioned encode → decode（强类型）
    summary = {
        "dsaProjection": encode_dsa_projection_to_summary(
            schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
            dsa_projection_payload=dict(artifact.payload.get("dsa") or {}),
            dsa_visual_contract=dict(artifact.visual or {}),
            availability=dict(artifact.availability or {}),
            parameter_hash=artifact.parameter_hash,
            source_core_run_id=str(artifact.source_core_run_id),
            algorithm_versions=dict(artifact.algorithm_versions or {}),
            input_hash="in-e2e",
            bars_hash="bars-e2e",
            adj_factor_hash="adj-e2e",
        )
    }
    decoded = decode_dsa_projection_from_summary(
        summary,
        instrument_id="i-e2e",
        trade_date=pd.Timestamp("2026-08-06").date(),
    )
    assert decoded.parameter_hash == artifact.parameter_hash
    assert decoded.source_core_run_id == "core-run-e2e"

    # 6. DSA projection（指标一致，不解析中文摘要）
    record = map_dsa_projection(
        decoded,
        requirement="required_compatibility",
        expected_core_run_id="core-run-e2e",
        expected_core_parameter_hash=artifact.parameter_hash,
        expected_dsa_version="dsa-v3",
    )
    # dsa_vwap 从 artifact 到 projection 一致（若 present）
    if "dsa_vwap" in artifact.payload.get("dsa", {}):
        assert record.payload["dsa_vwap"] == artifact.payload["dsa"]["dsa_vwap"]

    # 7. state-event candidates 至少为空列表（不伪造事件）
    assert isinstance(artifact.events, list)
