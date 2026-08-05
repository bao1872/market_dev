"""[V2.1 EPIC-04] Precomputed DSA Projection 合同单元测试。

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_dsa_projection_service.py -q -p no:cacheprovider
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain_status import (
    DSA_PROJECTION_REQUIREMENT_OPTIONAL,
    DSA_PROJECTION_REQUIREMENT_REQUIRED,
    DSA_PROJECTION_REQUIREMENT_RETIRED,
)
from app.services.core_run_context import CoreComputationArtifact
from app.services.dsa_projection_service import (
    DSAProjectionMappingError,
    DSAProjectionRecord,
    build_dsa_projection_payload,
    compute_projection_hash,
    is_projection_consumable,
    map_dsa_projection,
    reconcile_projection_parameter_hash,
)

_DSA_VERSION = "dsa-v1"
_PARAM_HASH = "param-hash-abc"


def _make_artifact(**overrides) -> CoreComputationArtifact:
    base = {
        "instrument_id": "600000",
        "trade_date": date(2026, 8, 4),
        "payload": {
            "dsa": {
                "dsa_dir_bars": 12,
                "regime_value": 1,
                "regime_strength": 0.003,
                "dsa_vwap": 10.5,
                "dsa_vwap_dev_pct": 1.2,
            }
        },
        "visual": {
            "dsa_vwap": 10.5,
            "regime_id": 1,
            "anchor_time": "2026-08-04",
            "pivot_labels": [{"time": "2026-08-04", "type": "pivot"}],
        },
        "availability": {"structure": "ready", "smc": "ready", "momentum": "ready"},
    }
    base.update(overrides)
    return CoreComputationArtifact(**base)


def test_maps_projection_without_runtime_execute():
    """从 artifact 直接映射，不调用 runtime.execute、不解析中文摘要。"""
    artifact = _make_artifact()
    rec = map_dsa_projection(
        artifact,
        dsa_version=_DSA_VERSION,
        parameter_hash=_PARAM_HASH,
        source_core_run_id="run-1",
    )
    assert isinstance(rec, DSAProjectionRecord)
    assert rec.dsa_version == _DSA_VERSION
    assert rec.requirement == DSA_PROJECTION_REQUIREMENT_REQUIRED
    # metrics 直接映射（数值，非中文摘要）
    assert rec.payload["dsa_dir_bars"] == 12
    assert rec.payload["regime_value"] == 1
    assert rec.payload["dsa_vwap"] == 10.5
    # visual 直接映射
    assert rec.visual["regime_id"] == 1
    assert "pivot_labels" in rec.visual


def test_projection_hash_is_stable_and_deterministic():
    """相同 metrics/版本/参数 → 相同 projection hash。"""
    a = map_dsa_projection(_make_artifact(), dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)
    b = map_dsa_projection(_make_artifact(), dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)
    assert a.projection_hash == b.projection_hash
    assert a.projection_hash == compute_projection_hash(
        a.payload, _DSA_VERSION, _PARAM_HASH
    )


def test_projection_hash_changes_when_metrics_change():
    """指标变化 → projection hash 变化。"""
    a = map_dsa_projection(_make_artifact(), dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)
    changed = _make_artifact(payload={"dsa": {"dsa_dir_bars": 13, "regime_value": 1, "dsa_vwap": 10.5}})
    b = map_dsa_projection(changed, dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)
    assert a.projection_hash != b.projection_hash


def test_missing_required_metric_raises():
    """缺必需指标（regime_value/dsa_vwap）→ 映射失败。"""
    artifact = _make_artifact(payload={"dsa": {"dsa_dir_bars": 12}})
    with pytest.raises(DSAProjectionMappingError):
        map_dsa_projection(artifact, dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)


def test_invalid_requirement_raises():
    """非法 requirement → 构建失败。"""
    artifact = _make_artifact()
    with pytest.raises(DSAProjectionMappingError):
        map_dsa_projection(
            artifact,
            dsa_version=_DSA_VERSION,
            parameter_hash=_PARAM_HASH,
            requirement="unknown_stage",
        )


def test_requirement_compatibility_stages():
    """兼容阶段消费门（E04-T03）。"""
    rec_required = map_dsa_projection(
        _make_artifact(), dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH
    )
    assert is_projection_consumable(rec_required, artifact_available=True)
    assert not is_projection_consumable(rec_required, artifact_available=False)

    rec_optional = map_dsa_projection(
        _make_artifact(),
        dsa_version=_DSA_VERSION,
        parameter_hash=_PARAM_HASH,
        requirement=DSA_PROJECTION_REQUIREMENT_OPTIONAL,
    )
    assert is_projection_consumable(rec_optional, artifact_available=False)

    rec_retired = map_dsa_projection(
        _make_artifact(),
        dsa_version=_DSA_VERSION,
        parameter_hash=_PARAM_HASH,
        requirement=DSA_PROJECTION_REQUIREMENT_RETIRED,
    )
    assert not is_projection_consumable(rec_retired, artifact_available=True)


def test_build_payload_serializable():
    """build_dsa_projection_payload 返回可序列化 dict。"""
    payload = build_dsa_projection_payload(
        _make_artifact(),
        dsa_version=_DSA_VERSION,
        parameter_hash=_PARAM_HASH,
        source_core_run_id="run-1",
    )
    assert payload["instrument_id"] == "600000"
    assert payload["trade_date"] == "2026-08-04"
    assert payload["source_core_run_id"] == "run-1"
    assert payload["projection_hash"]


def test_fallback_to_top_level_metrics():
    """artifact.payload 无 dsa 子字典时回退到 metrics/顶层。"""
    artifact = _make_artifact(
        payload={"metrics": {"dsa_dir_bars": 5, "regime_value": -1, "dsa_vwap": 9.9}}
    )
    rec = map_dsa_projection(artifact, dsa_version=_DSA_VERSION, parameter_hash=_PARAM_HASH)
    assert rec.payload["dsa_dir_bars"] == 5
    assert rec.payload["regime_value"] == -1


def test_reconcile_matching_parameter_hash_ok():
    """projection 参数 hash 与 core artifact 一致 → 对账通过。"""
    rec = map_dsa_projection(
        _make_artifact(),
        dsa_version=_DSA_VERSION,
        parameter_hash=_PARAM_HASH,
        source_core_run_id="run-1",
        expected_core_parameter_hash=_PARAM_HASH,
    )
    assert rec.parameter_hash == _PARAM_HASH


def test_reconcile_mismatch_raises():
    """projection 参数 hash 与 core artifact 不一致 → 拒绝映射（lineage 断裂）。"""
    with pytest.raises(DSAProjectionMappingError, match="参数 hash 与 core artifact 不一致"):
        map_dsa_projection(
            _make_artifact(),
            dsa_version=_DSA_VERSION,
            parameter_hash=_PARAM_HASH,
            expected_core_parameter_hash="different-core-hash",
        )


def test_reconcile_function_mismatch_raises():
    """直接调用 reconcile_projection_parameter_hash 不一致时抛错。"""
    with pytest.raises(DSAProjectionMappingError):
        reconcile_projection_parameter_hash("core-a", "proj-b")


def test_reconcile_function_match_ok():
    """对账一致时不抛错。"""
    reconcile_projection_parameter_hash(_PARAM_HASH, _PARAM_HASH)
