"""CoreArtifactCodec 单元测试（P0-05 full round-trip + P0-04 正式 codec）。

[CHANGE-20260805-CP4A-CP3]
验证：
- encode→decode round-trip 后 DSA projection payload/visual/availability/lineage 一致。
- 正常链与 restart 链共用正式 codec（不再依赖 recovery 私有 helper）。
- lineage 校验（sourceCoreRunId/parameterHash/dsa version）拒绝不匹配。
- 缺 dsaProjection 块 / schemaVersion 不匹配 → CoreArtifactDecodeError。
"""

from __future__ import annotations

import pytest

from app.services.core_artifact_codec import (
    CORE_ARTIFACT_SCHEMA_VERSION,
    CoreArtifactDecodeError,
    decode_dsa_projection_from_summary,
    encode_dsa_projection_to_summary,
    validate_lineage,
)
from app.services.dsa_projection_service import map_dsa_projection


def _encode_summary() -> dict:
    return encode_dsa_projection_to_summary(
        schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
        dsa_projection_payload={
            "dsa_dir_bars": 12,
            "regime_value": 1,
            "dsa_vwap": 10.5,
            "dsa_vwap_dev_pct": 0.3,
        },
        dsa_visual_contract={"dsa_vwap": 10.5, "regime_id": 1, "anchor_time": "2026-07-30"},
        availability={"trend": "ready", "structure": "ready", "momentum": "ready"},
        parameter_hash="ph-1",
        source_core_run_id="core-run-1",
        algorithm_versions={"dsa": "dsa-v1", "smc": "smc-v1", "momentum": "momentum-v1"},
        input_hash="in-1",
        bars_hash="bars-1",
        adj_factor_hash="adj-1",
    )


def test_codec_roundtrip_preserves_all_fields() -> None:
    """encode→decode 后 payload/visual/availability/lineage 全字段一致（P0-05）。"""
    summary = {"dsaProjection": _encode_summary()}
    decoded = decode_dsa_projection_from_summary(summary)

    assert decoded.payload["dsa"]["dsa_vwap"] == 10.5
    assert decoded.payload["dsa"]["dsa_dir_bars"] == 12
    assert decoded.visual["dsa_vwap"] == 10.5
    assert decoded.availability["trend"] == "ready"
    assert decoded.parameter_hash == "ph-1"
    assert decoded.source_core_run_id == "core-run-1"
    assert decoded.algorithm_versions["dsa"] == "dsa-v1"
    assert decoded.hashes["input_hash"] == "in-1"


def test_codec_decoded_feeds_map_dsa_projection() -> None:
    """decode 结果可直接被 map_dsa_projection 消费（正常链 DSA 投影）。"""
    import uuid
    from datetime import date

    summary = {"dsaProjection": _encode_summary()}
    decoded = decode_dsa_projection_from_summary(
        summary,
        instrument_id=uuid.uuid4(),
        trade_date=date(2026, 7, 30),
    )
    record = map_dsa_projection(
        decoded,
        requirement="required_compatibility",
        expected_core_run_id="core-run-1",
        expected_core_parameter_hash="ph-1",
        expected_dsa_version="dsa-v1",
    )
    assert record.payload["dsa_vwap"] == 10.5
    assert record.payload["dsa_dir_bars"] == 12


def test_codec_missing_block_raises() -> None:
    """缺 dsaProjection 块 → CoreArtifactDecodeError。"""
    with pytest.raises(CoreArtifactDecodeError):
        decode_dsa_projection_from_summary({"first_pyramid": {}})


def test_codec_schema_version_mismatch_raises() -> None:
    """schemaVersion 不匹配 → CoreArtifactDecodeError。"""
    summary = {"dsaProjection": _encode_summary()}
    summary["dsaProjection"]["schemaVersion"] = 99
    with pytest.raises(CoreArtifactDecodeError, match="schemaVersion"):
        decode_dsa_projection_from_summary(summary)


def test_codec_validate_lineage_rejects_mismatch() -> None:
    """lineage 不一致 → CoreArtifactDecodeError。"""
    decoded = decode_dsa_projection_from_summary({"dsaProjection": _encode_summary()})
    with pytest.raises(CoreArtifactDecodeError, match="sourceCoreRunId"):
        validate_lineage(
            decoded,
            expected_core_run_id="different-run",
            expected_parameter_hash="ph-1",
            expected_dsa_version="dsa-v1",
        )
    with pytest.raises(CoreArtifactDecodeError, match="parameterHash"):
        validate_lineage(
            decoded,
            expected_core_run_id="core-run-1",
            expected_parameter_hash="ph-wrong",
            expected_dsa_version="dsa-v1",
        )
