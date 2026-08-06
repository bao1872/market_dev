"""CoreArtifactCodec — 正式、版本化的 artifact 投影编解码（P0-05/P0-04）。

[CHANGE-20260805-CP4A-CP3]
正常 scheduled 主链与 restart 链**共同依赖**本 codec，禁止主链直接依赖
`granular_restart_service._artifact_from_snapshot`（那是 recovery 私有 helper）。

职责：
- encode：把 core artifact 的 DSA projection 持久化字段序列化进 snapshot summary_payload。
- decode：从 snapshot summary_payload 重建 DSA projection artifact（供 map_dsa_projection）。
- lineage 校验：schemaVersion、parameterHash、sourceCoreRunId、algorithmVersions 一致性。
- 不再从面向 UI 的 `continuousFactors` 反向拼装 DSA projection。

本模块为纯计算 + dict 编解码，不连数据库，可纯单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.core_run_context import CORE_ARTIFACT_SCHEMA_VERSION


@dataclass(frozen=True)
class DecodedCoreArtifact:
    """codec 解码出的强类型 core artifact（替代 SimpleNamespace，供 map_dsa_projection 消费）。

    [CHANGE-20260805-CP4A-CP3] 强类型约束：字段缺失在构造/解码时即失败，而非运行时。
    """

    payload: dict[str, Any]
    visual: dict[str, Any]
    availability: dict[str, str]
    parameter_hash: str | None
    source_core_run_id: str | None
    algorithm_versions: dict[str, str]
    hashes: dict[str, str | None] = field(default_factory=dict)
    instrument_id: Any = None
    trade_date: Any = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CORE_ARTIFACT_SCHEMA_VERSION

    def validate_lineage(
        self,
        *,
        expected_core_run_id: Any = None,
        expected_core_parameter_hash: str | None = None,
        expected_dsa_version: str | None = None,
    ) -> None:
        """lineage 一致性校验（与 CoreComputationArtifact.validate_lineage 语义一致）。"""
        validate_lineage(
            self,
            expected_core_run_id=expected_core_run_id,
            expected_parameter_hash=expected_core_parameter_hash,
            expected_dsa_version=expected_dsa_version,
        )


@dataclass(frozen=True)
class DecodedCoreComputationArtifact:
    """完整 core artifact 的强类型解码结果（P0-05 full core / CP4A.2 Step2）。

    不再返回 raw dict：字段缺失在构造时即失败；schema/lineage/hashes 严格校验。
    """

    schema_version: int
    first_pyramid_core: dict[str, Any]
    structural_payload: dict[str, Any]
    dsa_projection: dict[str, Any]
    state_event_candidates: tuple[dict[str, Any], ...]
    availability: dict[str, str]
    hashes: dict[str, str | None]
    lineage: dict[str, Any]
    diagnostics: dict[str, Any]
    instrument_id: Any = None
    trade_date: Any = None


class CoreArtifactDecodeError(ValueError):
    """artifact 编解码或 lineage 校验失败。"""


def encode_dsa_projection_to_summary(
    *,
    schema_version: int,
    dsa_projection_payload: dict[str, Any],
    dsa_visual_contract: dict[str, Any],
    availability: dict[str, str],
    parameter_hash: str | None,
    source_core_run_id: str | None,
    algorithm_versions: dict[str, str],
    input_hash: str | None,
    bars_hash: str | None,
    adj_factor_hash: str | None,
) -> dict[str, Any]:
    """把 artifact 的 DSA projection 字段编码为 versioned summary 块（P0-05）。"""
    return {
        "schemaVersion": schema_version,
        "dsaProjectionPayload": dict(dsa_projection_payload or {}),
        "dsaVisualContract": dict(dsa_visual_contract or {}),
        "availability": dict(availability or {}),
        "lineage": {
            "parameterHash": parameter_hash,
            "sourceCoreRunId": source_core_run_id,
            "algorithmVersions": dict(algorithm_versions or {}),
            "inputHash": input_hash,
            "barsHash": bars_hash,
            "adjFactorHash": adj_factor_hash,
        },
    }


def encode_core_artifact_to_summary(
    *,
    schema_version: int,
    first_pyramid_core: dict[str, Any],
    structural_payload: dict[str, Any],
    dsa_projection_payload: dict[str, Any],
    dsa_visual_contract: dict[str, Any],
    state_event_candidates: list[dict[str, Any]],
    availability: dict[str, str],
    parameter_hash: str | None,
    source_core_run_id: str | None,
    algorithm_versions: dict[str, str],
    input_hash: str | None,
    bars_hash: str | None,
    adj_factor_hash: str | None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把完整 CoreComputationArtifact 编码为 versioned summary 块（P0-05 full core）。

    [CHANGE-20260806-CP4A.1 / Item 5] 正常链与 restart 链据此统一持久化完整 core artifact，
    不只 DSA projection。schemaVersion 为独立 core artifact schema 版本。
    """
    return {
        "coreArtifactSchemaVersion": schema_version,
        "firstPyramidCore": dict(first_pyramid_core or {}),
        "structuralPayload": dict(structural_payload or {}),
        "dsaProjection": {
            "dsaProjectionPayload": dict(dsa_projection_payload or {}),
            "dsaVisualContract": dict(dsa_visual_contract or {}),
        },
        "stateEventCandidates": list(state_event_candidates or []),
        "availability": dict(availability or {}),
        "hashes": {
            "inputHash": input_hash,
            "barsHash": bars_hash,
            "adjFactorHash": adj_factor_hash,
        },
        "lineage": {
            "parameterHash": parameter_hash,
            "sourceCoreRunId": source_core_run_id,
            "algorithmVersions": dict(algorithm_versions or {}),
        },
        "diagnostics": dict(diagnostics or {}),
    }


def decode_dsa_projection_from_summary(
    summary_payload: dict[str, Any],
    *,
    instrument_id: Any = None,
    trade_date: Any = None,
) -> DecodedCoreArtifact:
    """从 snapshot summary_payload 的 `dsaProjection` 重建强类型 DSA projection artifact。

    返回 `DecodedCoreArtifact`（强类型 dataclass，字段缺失在构造时即失败）。
    instrument_id/trade_date 由调用方（snapshot 行）提供。

    Raises:
        CoreArtifactDecodeError: 缺 dsaProjection 块、schemaVersion 不匹配、lineage 缺失
    """
    block = (summary_payload or {}).get("dsaProjection")
    if not isinstance(block, dict):
        # [CHANGE-20260806 / PG-暴露缺陷] 向后兼容：旧版本 snapshot（如 seed 写的）summary 只含
        # first_pyramid.trend.continuousFactors，无 dsaProjection 块。从 first_pyramid 重建 DSA
        # projection（dsa_vwap/regime_value/dsa_dir_bars 在 continuousFactors 中），避免硬失败。
        fp = (summary_payload or {}).get("first_pyramid") or {}
        trend = fp.get("trend") or {}
        cf = trend.get("continuousFactors") or {}
        if cf:
            return DecodedCoreArtifact(
                payload={"dsa": dict(cf)},
                visual={"dsa_vwap": cf.get("dsa_vwap")},
                availability=dict(fp.get("fieldAvailability") or {}),
                hashes={},
                parameter_hash=fp.get("parameterHash"),
                source_core_run_id=fp.get("sourceRunId"),
                algorithm_versions=dict(fp.get("algorithmVersions") or {}),
                instrument_id=instrument_id,
                trade_date=trade_date,
            )
        raise CoreArtifactDecodeError(
            "summary_payload 缺 dsaProjection 块且无 first_pyramid 可回退（需重算）"
        )
    schema_version = block.get("schemaVersion")
    if schema_version != CORE_ARTIFACT_SCHEMA_VERSION:
        raise CoreArtifactDecodeError(
            f"dsaProjection schemaVersion 不匹配: {schema_version} != "
            f"{CORE_ARTIFACT_SCHEMA_VERSION}"
        )
    lineage = block.get("lineage") or {}
    if not lineage.get("sourceCoreRunId"):
        raise CoreArtifactDecodeError("dsaProjection lineage 缺 sourceCoreRunId")

    return DecodedCoreArtifact(
        payload={"dsa": dict(block.get("dsaProjectionPayload") or {})},
        visual=dict(block.get("dsaVisualContract") or {}),
        availability=dict(block.get("availability") or {}),
        hashes={
            "input_hash": lineage.get("inputHash"),
            "bars_hash": lineage.get("barsHash"),
            "adj_factor_hash": lineage.get("adjFactorHash"),
        },
        parameter_hash=lineage.get("parameterHash"),
        source_core_run_id=lineage.get("sourceCoreRunId"),
        algorithm_versions=dict(lineage.get("algorithmVersions") or {}),
        instrument_id=instrument_id,
        trade_date=trade_date,
    )


def decode_core_artifact_from_summary(
    summary_payload: dict[str, Any],
    *,
    instrument_id: Any = None,
    trade_date: Any = None,
) -> DecodedCoreComputationArtifact:
    """从 summary 的 `coreArtifact` 块解码**强类型**完整 core artifact（CP4A.2 Step2）。

    返回 DecodedCoreComputationArtifact（frozen dataclass），字段缺失在构造时即失败。

    Raises:
        CoreArtifactDecodeError: 缺 coreArtifact 块 / schemaVersion 不匹配 / 必选字段缺失
    """
    block = (summary_payload or {}).get("coreArtifact")
    if not isinstance(block, dict):
        raise CoreArtifactDecodeError("summary_payload 缺 coreArtifact 块")
    schema_version = block.get("coreArtifactSchemaVersion")
    if schema_version != CORE_ARTIFACT_SCHEMA_VERSION:
        raise CoreArtifactDecodeError(
            f"coreArtifact schemaVersion 不匹配: {schema_version} != "
            f"{CORE_ARTIFACT_SCHEMA_VERSION}"
        )
    lineage = block.get("lineage") or {}
    if not lineage.get("sourceCoreRunId"):
        raise CoreArtifactDecodeError("coreArtifact lineage 缺 sourceCoreRunId")
    if not isinstance(block.get("stateEventCandidates"), list):
        raise CoreArtifactDecodeError("coreArtifact 缺 stateEventCandidates")
    return DecodedCoreComputationArtifact(
        schema_version=schema_version,
        first_pyramid_core=dict(block.get("firstPyramidCore") or {}),
        structural_payload=dict(block.get("structuralPayload") or {}),
        dsa_projection=dict(block.get("dsaProjection") or {}),
        state_event_candidates=tuple(
            e for e in block.get("stateEventCandidates") or [] if isinstance(e, dict)
        ),
        availability=dict(block.get("availability") or {}),
        hashes=dict(block.get("hashes") or {}),
        lineage=dict(lineage),
        diagnostics=dict(block.get("diagnostics") or {}),
        instrument_id=instrument_id,
        trade_date=trade_date,
    )


def validate_lineage(
    decoded: DecodedCoreArtifact,
    *,
    expected_core_run_id: str | None,
    expected_parameter_hash: str | None,
    expected_dsa_version: str | None,
) -> None:
    """lineage 一致性校验（P0-05 Step 5：version/config/hash 一致）。

    Raises:
        CoreArtifactDecodeError: 任一 lineage 字段与预期不匹配
    """
    if expected_core_run_id and decoded.source_core_run_id != expected_core_run_id:
        raise CoreArtifactDecodeError(
            "lineage sourceCoreRunId 不匹配: "
            f"{decoded.source_core_run_id} != {expected_core_run_id}"
        )
    if expected_parameter_hash and decoded.parameter_hash != expected_parameter_hash:
        raise CoreArtifactDecodeError(
            "lineage parameterHash 不匹配: "
            f"{decoded.parameter_hash} != {expected_parameter_hash}"
        )
    if expected_dsa_version:
        actual = (decoded.algorithm_versions or {}).get("dsa")
        if actual != expected_dsa_version:
            raise CoreArtifactDecodeError(
                f"lineage dsa version 不匹配: {actual} != {expected_dsa_version}"
            )
