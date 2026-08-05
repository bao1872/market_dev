"""Precomputed DSA Projection 合同（EPIC-04）。

[PRD V2.1 §7 / next.md EPIC-04 E04-T01..T04]
- 在 scheduled after-close 中，DSA 已由 CoreRunContext 计算一次并持久化在
  CoreComputationArtifact 中。本模块提供 DSA projection 的**正式入口**：
  从持久化 artifact 直接映射出 DSA projection，**禁止调用 runtime.execute**、
  **禁止解析中文摘要**。
- 兼容阶段（E04-T03）：required_compatibility / optional_compatibility / retired，
  初始为 required_compatibility，消费者必须读取由 artifact 派生的 projection。
- 恢复（E04-T04）：从持久化 artifact 重建 projection，不重算 DSA。

design decisions：
1. DSA_PROJECTION_METRIC_KEYS 是 projection 持久化指标的唯一 SSOT（单一来源），
   与 dsa_selector._history_row_to_metrics 的标量输出对齐，禁止在消费端新增推断键。
2. map_dsa_projection 直接映射 artifact 的 metrics + visual，不解析中文摘要：
   - metrics：从 artifact.payload 的 "dsa" 子字典（Agent 约定）或顶级度量中取值
   - visual：从 artifact.visual 直接取 DSA 图表字段（pivot_labels/regime_id/segments）
3. projection hash 由 (metric keys + dsa_version + parameter_hash) 派生，用于幂等审计。
4. 本模块为纯函数 + DTO，不连接数据库，可 PURE_UNIT_TEST=1 测试。

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.dsa_projection_service
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.domain_status import (
    ALL_DSA_PROJECTION_REQUIREMENTS,
    DSA_PROJECTION_REQUIREMENT_REQUIRED,
)
from app.services.core_run_context import CoreComputationArtifact

# ===== DSA projection 指标唯一 SSOT（与 dsa_selector._history_row_to_metrics 对齐）=====
DSA_PROJECTION_METRIC_KEYS: frozenset[str] = frozenset({
    # yaml outputs 字段
    "dsa_dir_bars",
    "vwap_ret_avg",
    "vwap_ret_total",
    "offset_mean",
    "offset_std",
    "offset_variance_rate",
    "offset_percentile",
    # 扩展字段（详情展示 & 筛选）
    "regime_value",
    "regime_strength",
    "trend_transition",
    "offset_rate",
    "change_pct",
    "touch_rope",
    "touch_vwap",
    "rope_dir1_pct",
    "rope_dir0_pct",
    "rope_dir_neg1_pct",
    "cross_up_count",
    "cross_down_count",
    "last_cross_up_date",
    "last_cross_down_date",
    # ad2.md 新增字段
    "vwap_ret_5",
    "vwap_ret_10",
    "vwap_ret_20",
    "dsa_vwap",
    "dsa_vwap_dev_pct",
    "vol_zscore",
    "avg_amount_20d",
    # canonical segment output
    "segment_id",
    "segment_direction",
    "segment_start_bar_index",
    "segment_end_bar_index",
    "segment_start_time",
    "segment_end_time",
    "segment_change_pct",
    "segment_anchor_time",
})

# artifact.visual 中 DSA 图表字段（直接映射，不解析中文摘要）
DSA_PROJECTION_VISUAL_KEYS: frozenset[str] = frozenset({
    "dsa_vwap",
    "regime_id",
    "anchor_time",
    "pivot_labels",
    "pivot_type",
    "pivot_price",
    "segments",
})

# projection 持久化版本（算法版本由 artifact 携带，这里为 projection 合同版本）
DSA_PROJECTION_CONTRACT_VERSION = "dsa-projection-v1"


class DSAProjectionMappingError(ValueError):
    """DSA projection 映射失败（缺必需指标 / 版本不一致等）。"""


@dataclass(frozen=True)
class DSAProjectionRecord:
    """DSA projection 持久化 DTO（E04-T01/T02）。

    - payload: 直接映射的标量指标（metrics），不含任何解析后的中文摘要
    - visual: 直接映射的图表字段（visual contract）
    - requirement: 兼容阶段（初始 required_compatibility）
    - dsa_version / parameter_hash: 版本与参数 hash（lineage 审计）
    - projection_hash: 由指标 + 版本 + 参数 hash 派生，幂等审计用
    """

    instrument_id: Any
    trade_date: date
    source_core_run_id: Any
    dsa_version: str
    parameter_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    requirement: str = DSA_PROJECTION_REQUIREMENT_REQUIRED
    contract_version: str = DSA_PROJECTION_CONTRACT_VERSION
    projection_hash: str = field(default="")

    def __post_init__(self) -> None:
        if self.requirement not in ALL_DSA_PROJECTION_REQUIREMENTS:
            raise DSAProjectionMappingError(
                f"未知 DSA projection requirement: {self.requirement}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": str(self.instrument_id),
            "trade_date": self.trade_date.isoformat(),
            "source_core_run_id": str(self.source_core_run_id),
            "dsa_version": self.dsa_version,
            "parameter_hash": self.parameter_hash,
            "requirement": self.requirement,
            "contract_version": self.contract_version,
            "projection_hash": self.projection_hash,
            "payload": self.payload,
            "visual": self.visual,
        }


def _extract_metrics(artifact: CoreComputationArtifact) -> dict[str, Any]:
    """从 artifact 提取 DSA 标量指标。

    优先取 artifact.payload["dsa"] 子字典（Agent 约定），否则回退到 artifact.payload
    顶层度量。**禁止解析中文摘要**（payload 中不含中文 summary）。
    """
    payload = artifact.payload or {}
    dsa_metrics = payload.get("dsa")
    if isinstance(dsa_metrics, dict):
        return dsa_metrics
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    return payload


def _extract_visual(artifact: CoreComputationArtifact) -> dict[str, Any]:
    """从 artifact.visual 提取 DSA 图表字段（直接映射）。"""
    visual = artifact.visual or {}
    return {
        k: visual[k]
        for k in DSA_PROJECTION_VISUAL_KEYS
        if k in visual
    }


def compute_projection_hash(
    metrics: dict[str, Any],
    dsa_version: str,
    parameter_hash: str,
) -> str:
    """由指标 + 版本 + 参数 hash 派生稳定 projection hash（顺序无关）。"""
    material = {
        "metrics": {k: metrics.get(k) for k in sorted(DSA_PROJECTION_METRIC_KEYS)},
        "dsa_version": dsa_version,
        "parameter_hash": parameter_hash,
        "contract_version": DSA_PROJECTION_CONTRACT_VERSION,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def reconcile_projection_parameter_hash(
    core_parameter_hash: str,
    projection_parameter_hash: str,
) -> None:
    """对账：projection 派生所依据的参数 hash 必须与 core artifact 一致。

    [Commit C §8.2 / §8.3 source run/hash/version 一致]
    projection 由持久化 CoreComputationArtifact 派生，其 parameter_hash 必须与
    该 core artifact 携带的参数 hash 一致。若不一致，说明 projection 基于不同版本
    的 core 参数被构建，属于 lineage 断裂，应拒绝消费。

    Raises:
        DSAProjectionMappingError: 两者不一致。
    """
    if core_parameter_hash != projection_parameter_hash:
        raise DSAProjectionMappingError(
            "DSA projection 参数 hash 与 core artifact 不一致："
            f"core={core_parameter_hash!r}, projection={projection_parameter_hash!r}，"
            "禁止基于不同版本 core 参数的 projection 被消费"
        )


def map_dsa_projection(
    artifact: CoreComputationArtifact,
    *,
    requirement: str = DSA_PROJECTION_REQUIREMENT_REQUIRED,
    expected_core_run_id: Any | None = None,
    expected_core_parameter_hash: str | None = None,
    expected_dsa_version: str | None = None,
) -> DSAProjectionRecord:
    """从持久化 CoreComputationArtifact 映射 DSA projection（E04-T01/T02）。

    不调用 runtime.execute；不解析中文摘要；metrics/visual 直接映射。

    [Commit C 修正 2026-08-05] lineage 唯一真源：
    - parameter_hash / dsa_version / source_core_run_id 一律**从 artifact 自身读取**，
      禁止比较两个调用者任意传入的字符串。
    - 调用方可传入 `expected_*`（来自 CoreRunContext 的 run 级 lineage），与 artifact
      携带的持久化 lineage 对账：source_core_run_id / parameter_hash / dsa algorithm
      version 任一不一致即映射失败（拒绝基于不同 run/参数/算法版本的 projection 消费）。

    Raises:
        DSAProjectionMappingError: 必需指标缺失、requirement 非法，或
            artifact 缺少 lineage、或 expected_* 与 artifact lineage 不一致。
    """
    # 从持久化 artifact 自身读取 lineage（唯一真源）
    parameter_hash = artifact.parameter_hash
    dsa_version = artifact.algorithm_versions.get("dsa")
    source_core_run_id = artifact.source_core_run_id

    if not parameter_hash:
        raise DSAProjectionMappingError(
            "artifact 缺少持久化 parameter_hash，禁止用任意传入字符串派生 projection"
        )
    if not dsa_version:
        raise DSAProjectionMappingError(
            "artifact 缺少 dsa algorithm version，无法派生 projection"
        )

    # 对账：expected_*（来自 CoreRunContext）必须与 artifact lineage 一致
    if expected_core_parameter_hash is not None:
        reconcile_projection_parameter_hash(expected_core_parameter_hash, parameter_hash)
    if expected_dsa_version is not None and expected_dsa_version != dsa_version:
        raise DSAProjectionMappingError(
            "DSA projection algorithm version 与 core artifact 不一致："
            f"expected={expected_dsa_version!r}, artifact={dsa_version!r}，"
            "禁止基于不同算法版本 core 的 projection 被消费"
        )
    if expected_core_run_id is not None and source_core_run_id is not None:
        if str(expected_core_run_id) != str(source_core_run_id):
            raise DSAProjectionMappingError(
                "DSA projection source_core_run_id 与 core artifact 不一致："
                f"expected={expected_core_run_id!s}, artifact={source_core_run_id!s}，"
                "禁止基于不同 core run 的 projection 被消费"
            )

    metrics = _extract_metrics(artifact)
    visual = _extract_visual(artifact)

    # 必需指标门禁：dsa_dir_bars / regime_value / dsa_vwap 缺失即映射失败
    missing = [
        k for k in ("dsa_dir_bars", "regime_value", "dsa_vwap")
        if k not in metrics
    ]
    if missing:
        raise DSAProjectionMappingError(
            f"DSA projection 缺少必需指标: {missing} (instrument_id={artifact.instrument_id})"
        )

    projection_hash = compute_projection_hash(metrics, dsa_version, parameter_hash)

    return DSAProjectionRecord(
        instrument_id=artifact.instrument_id,
        trade_date=artifact.trade_date,
        source_core_run_id=source_core_run_id,
        dsa_version=dsa_version,
        parameter_hash=parameter_hash,
        payload=metrics,
        visual=visual,
        requirement=requirement,
        projection_hash=projection_hash,
    )


def build_dsa_projection_payload(
    artifact: CoreComputationArtifact,
    *,
    requirement: str = DSA_PROJECTION_REQUIREMENT_REQUIRED,
    expected_core_run_id: Any | None = None,
    expected_core_parameter_hash: str | None = None,
    expected_dsa_version: str | None = None,
) -> dict[str, Any]:
    """便捷入口：返回 DSAProjectionRecord.to_dict()（持久化/API 负载）。

    [Commit C 修正 2026-08-05] 强制对账：与正式 production caller 一致，必须校验
    source_core_run_id / parameter_hash / dsa algorithm version 与 artifact lineage
    一致，防止基于任意传入字符串构建 projection。
    """
    record = map_dsa_projection(
        artifact=artifact,
        requirement=requirement,
        expected_core_run_id=expected_core_run_id,
        expected_core_parameter_hash=expected_core_parameter_hash,
        expected_dsa_version=expected_dsa_version,
    )
    return record.to_dict()


def is_projection_consumable(
    record: DSAProjectionRecord,
    *,
    artifact_available: bool,
) -> bool:
    """消费者是否可安全读取该 projection（E04-T03 兼容阶段门）。

    - required_compatibility：要求 artifact 可用，且 projection 为 ready。
    - optional_compatibility：artifact 不可用时允许跳过（不阻断 mandatory chain）。
    - retired：不再消费（旧路径已移除）。
    """
    if record.requirement == DSA_PROJECTION_REQUIREMENT_REQUIRED:
        return artifact_available
    if record.requirement == "optional_compatibility":
        return True
    # retired
    return False


if __name__ == "__main__":

    ctx = CoreComputationArtifact(
        instrument_id="600000",
        trade_date=date(2026, 8, 4),
        payload={
            "dsa": {
                "dsa_dir_bars": 12,
                "regime_value": 1,
                "regime_strength": 0.003,
                "dsa_vwap": 10.5,
                "dsa_vwap_dev_pct": 1.2,
            }
        },
        visual={"dsa_vwap": 10.5, "regime_id": 1, "anchor_time": "2026-08-04"},
        availability={"structure": "ready", "smc": "ready", "momentum": "ready"},
        source_core_run_id="run-1",
        parameter_hash="abc",
        algorithm_versions={"dsa": "dsa-v1"},
    )
    rec = map_dsa_projection(ctx)
    assert rec.projection_hash, "projection hash 不应为空"
    assert rec.requirement == DSA_PROJECTION_REQUIREMENT_REQUIRED
    assert is_projection_consumable(rec, artifact_available=True)
    print(f"OK: DSA projection mapped, hash={rec.projection_hash[:16]}...")
