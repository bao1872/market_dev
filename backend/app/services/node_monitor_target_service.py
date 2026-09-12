"""Node Monitor Target Set / Versioning（Stage C2）。

本模块是 Monitor V2 接入前的 **纯 target serialization / version boundary**。
它不计算 Node Profile、不触碰 crossing、不修改 `node_cluster_engine`。

职责边界（fail-closed）：
- 消费 C1 `NodeClusterInput`（已 qfq / 已带 Context 坐标）+
  `NodeClusterProfileResult`（由上游 orchestration 在正确复权坐标上计算）；
- 校验两者身份一致性（source identity / factor coordinate / business-date /
  profile bar-count / canonical DTO invariant）；
- 从 Canonical Node DTO（`build_node_regions`）按 **价格坐标** 去重生成
  `NodeMonitorTarget`，而非使用 `peak_000` 这类顺序位置 ID；
- 绑定四类身份（RAW source / adjustment coordinate / Node algorithm-profile
  contract / Canonical targets）生成确定性 64 位 SHA256 `target_set_version`；
- 生成 version-scoped `target_id`（64 位 SHA256），保证「同一 Target Set +
  同一价格 → 同一 ID；新 completed 15m / 公司行为 / 算法合同变化 → 新 ID」。

设计约束（来自 C2 spec）：
- Context mode 是硬前提：legacy `adjustment_context_hash is None` 直接拒绝；
- C1 `NodeClusterInput` source hash 是 MDAS **raw source identity**；
  engine `profile.daily_source_hash / bars_15m_source_hash` 是 qfq 内容 hash；
  **两者不得做相等断言**；
- Target Set 完全 deterministic：禁止任何墙钟时间或创建时间戳进入 version；
- 禁止任何 DB / MDAS / 外部缓存 / XDXR / realtime / 1m / monitor-state I/O。

用法（未来 Monitor V2 orchestration）：
    from app.services.node_monitor_target_service import (
        NodeMonitorTargetService,
    )
    target_set = NodeMonitorTargetService.build_target_set(node_input, profile)
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    build_node_regions,
    compute_node_regions_hash,
)
from app.services.node_cluster_input_provider import NodeClusterInput

# Monitor Target Set 自己的序列化/身份合同。
# 不要借用 NODE_CLUSTER_OUTPUT_SCHEMA_VERSION（Node Cluster output schema 语义不同）。
_NODE_MONITOR_TARGET_SCHEMA_VERSION = "node-monitor-target-set.v1"

# 价格规范化精度（与 Canonical Node DTO 的 4 位小数一致）
_PRICE_PRECISION = 4


class NodeMonitorTargetUnavailableError(RuntimeError):
    """Node Monitor Target Set 不可用（fail-closed，禁止 silent fallback）。

    reason 取值（与 spec §7-§42 一一对应）：
    - adjustment_context_hash_missing
    - node_input_unavailable
    - daily_source_hash_missing / m15_source_hash_missing
    - factor_hash_missing / node_input_factor_hash_mismatch / profile_factor_hash_mismatch
    - adjustment_as_of_missing / profile_adjustment_as_of_mismatch
    - profile_daily_count_mismatch / profile_m15_count_mismatch
    - profile_algorithm_version_missing / profile_contract_fingerprint_missing /
      profile_hash_missing / profile_hash_invalid
    - canonical_target_set_mismatch
    - invalid_target_price
    """

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "node monitor target unavailable "
            f"reason={reason}"
        )


@dataclass(frozen=True)
class NodeMonitorTarget:
    """单个 Monitor crossing 目标（价格坐标，非 UI 位置 ID）。

    Attributes:
        target_id: version-scoped 64 位 SHA256（target_set_version + price 派生）
        kind: "peak"（当前唯一 Node 类型；保留枚举便于扩展）
        price: 规范化 4 位小数价格坐标
        source_region_ids: 生成该 target 的 Canonical region entity_id 列表
            （audit/reference metadata，**不是** 长期 entity identity）
    """

    target_id: str
    kind: str
    price: float
    source_region_ids: tuple[str, ...]


@dataclass(frozen=True)
class NodeMonitorTargetSet:
    """稳定的 Node Monitor Target Set（不可变）。

    绑定四类身份：
    A. RAW market source: daily_source_hash / m15_source_hash
    B. adjustment coordinate: adjustment_context_hash / factor_hash
    C. Node algorithm/profile contract: algorithm_version /
       output_schema_version / contract_fingerprint / profile_hash
    D. Canonical targets: node_regions_hash + target price list

    Attributes:
        target_set_version: 64 位 SHA256，绑定上述四类身份
        targets: 按价格升序的 NodeMonitorTarget 元组
        daily_source_hash: C1 NodeClusterInput RAW daily source identity
        m15_source_hash: C1 NodeClusterInput RAW 15m source identity
        adjustment_context_hash: C1 Context 版本身份
        factor_hash: 复权因子坐标（daily == m15 == Context.factor_hash）
        algorithm_version: profile.algorithm_version
        output_schema_version: profile.output_schema_version
        contract_fingerprint: profile.contract_fingerprint
        profile_hash: profile.profile_hash
        node_regions_hash: Canonical Node DTO 内容 hash
        updated_through: 本 Set 使用到的最后一根 completed 15m bar 时间
            （诊断/刷新 watermark，**不进入 version hash**）
        input_availability: node_input.availability（available / degraded）
        input_degraded_reason: node_input.degraded_reason（诊断字段）
    """

    target_set_version: str
    targets: tuple[NodeMonitorTarget, ...]

    daily_source_hash: str
    m15_source_hash: str
    adjustment_context_hash: str
    factor_hash: str

    algorithm_version: str
    output_schema_version: int
    contract_fingerprint: str
    profile_hash: str
    node_regions_hash: str

    updated_through: pd.Timestamp
    input_availability: str
    input_degraded_reason: str | None

    @property
    def all_peak_prices(self) -> tuple[float, ...]:
        """所有 target 价格坐标（升序，与 Canonical DTO 一致）。"""
        return tuple(target.price for target in self.targets)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe 序列化（json.dumps(..., allow_nan=False) 必须成功）。"""
        return {
            "schema_version": _NODE_MONITOR_TARGET_SCHEMA_VERSION,
            "target_set_version": self.target_set_version,
            "targets": [
                {
                    "target_id": t.target_id,
                    "kind": t.kind,
                    "price": t.price,
                    "source_region_ids": list(t.source_region_ids),
                }
                for t in self.targets
            ],
            "all_peak_prices": list(self.all_peak_prices),
            "daily_source_hash": self.daily_source_hash,
            "m15_source_hash": self.m15_source_hash,
            "adjustment_context_hash": self.adjustment_context_hash,
            "factor_hash": self.factor_hash,
            "algorithm_version": self.algorithm_version,
            "output_schema_version": self.output_schema_version,
            "contract_fingerprint": self.contract_fingerprint,
            "profile_hash": self.profile_hash,
            "node_regions_hash": self.node_regions_hash,
            "updated_through": self.updated_through.isoformat(),
            "input_availability": self.input_availability,
            "input_degraded_reason": self.input_degraded_reason,
        }


def _stable_sha256(payload: dict[str, Any]) -> str:
    """确定性 SHA256（完整 64 位 hex）。

    使用 ensure_ascii=False + sort_keys=True + 紧凑分隔符，保证同 payload → 同 hash。
    """
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class NodeMonitorTargetService:
    """纯函数式 Target Set 构建服务（无状态、无 I/O）。"""

    @classmethod
    def build_target_set(
        cls,
        node_input: NodeClusterInput,
        profile: NodeClusterProfileResult,
    ) -> NodeMonitorTargetSet:
        """从 C1 NodeClusterInput + NodeClusterProfileResult 构建 Target Set。

        完全 deterministic：相同输入 → 相同 target_set_version / target_id。
        任何身份一致性失败 → 抛 `NodeMonitorTargetUnavailableError`（fail-closed）。

        Args:
            node_input: C1 已构建的 NodeClusterInput（Context mode 硬前提）
            profile: 上游 orchestration 在正确复权坐标上计算的 NodeClusterProfileResult

        Returns:
            NodeMonitorTargetSet（不可变）

        Raises:
            NodeMonitorTargetUnavailableError: 任意身份/坐标/合同不一致
        """
        # §7 Context-mode 硬前提：C2 不允许建立在无 Context 坐标的 Legacy Node input 上
        if node_input.adjustment_context_hash is None:
            raise NodeMonitorTargetUnavailableError(
                reason="adjustment_context_hash_missing"
            )

        # §8 availability：unavailable 直接拒绝；degraded 允许构建（仅记录诊断字段）
        if node_input.availability == "unavailable":
            raise NodeMonitorTargetUnavailableError(
                reason="node_input_unavailable"
            )

        # §9 RAW source identity 必须非空（使用 C1 的 RAW source hash，非 profile qfq hash）
        if not node_input.daily_source_hash:
            raise NodeMonitorTargetUnavailableError(
                reason="daily_source_hash_missing"
            )
        if not node_input.m15_source_hash:
            raise NodeMonitorTargetUnavailableError(
                reason="m15_source_hash_missing"
            )

        # §10 Factor coordinate 一致性（Context mode 下 daily == m15 == Context.factor_hash）
        if not node_input.daily_adj_factor_hash:
            raise NodeMonitorTargetUnavailableError(reason="factor_hash_missing")
        if node_input.daily_adj_factor_hash != node_input.m15_adj_factor_hash:
            raise NodeMonitorTargetUnavailableError(
                reason="node_input_factor_hash_mismatch"
            )
        factor_hash = node_input.daily_adj_factor_hash
        if profile.adj_factor_hash != factor_hash:
            raise NodeMonitorTargetUnavailableError(
                reason="profile_factor_hash_mismatch"
            )

        # §11 business-date 一致性（强制后续 Monitor orchestration 使用正确坐标计算 Profile）
        if node_input.adjustment_as_of is None:
            raise NodeMonitorTargetUnavailableError(
                reason="adjustment_as_of_missing"
            )
        expected_as_of = node_input.adjustment_as_of.isoformat()
        if profile.adjustment_as_of != expected_as_of:
            raise NodeMonitorTargetUnavailableError(
                reason="profile_adjustment_as_of_mismatch"
            )

        # §12 Profile bar-count consistency（证明 Profile 确实来自这份 Node Input）
        if profile.daily_bars_count != len(node_input.daily_bars):
            raise NodeMonitorTargetUnavailableError(
                reason="profile_daily_count_mismatch"
            )
        if profile.bars_15m_count != len(node_input.bars_15m):
            raise NodeMonitorTargetUnavailableError(
                reason="profile_m15_count_mismatch"
            )

        # §25 profile metadata 基本有效
        if not profile.algorithm_version:
            raise NodeMonitorTargetUnavailableError(
                reason="profile_algorithm_version_missing"
            )
        if not profile.contract_fingerprint:
            raise NodeMonitorTargetUnavailableError(
                reason="profile_contract_fingerprint_missing"
            )
        if not profile.profile_hash:
            raise NodeMonitorTargetUnavailableError(reason="profile_hash_missing")
        # profile_hash == "empty" 只在真正空 Profile 才允许；
        # 非空 profile_rows 却 hash 为 "empty" 是非法状态（可能被篡改/异常）
        if (
            node_input.availability != "unavailable"
            and profile.profile_rows
            and profile.profile_hash == "empty"
        ):
            raise NodeMonitorTargetUnavailableError(reason="profile_hash_invalid")

        # §13 使用现有 Canonical Node DTO（不再拼另一份 Node Region）
        regions = build_node_regions(profile)
        node_regions_hash = compute_node_regions_hash(regions)

        # §15 非有限价格必须 fail-closed（来自 all_peak_prices 或 canonical region mid）
        for price in profile.all_peak_prices:
            if not math.isfinite(float(price)):
                raise NodeMonitorTargetUnavailableError(reason="invalid_target_price")
        for region in regions:
            if not math.isfinite(float(region["mid"])):
                raise NodeMonitorTargetUnavailableError(reason="invalid_target_price")

        # §14 Canonical DTO 与旧 crossing 语义（all_peak_prices）必须完全一致
        # 保证 C2 没有偷偷改变旧 Monitor 的 Peak crossing 集合
        legacy_prices = tuple(
            sorted({
                round(float(price), _PRICE_PRECISION)
                for price in profile.all_peak_prices
            })
        )
        region_prices = tuple(
            sorted({
                round(float(region["mid"]), _PRICE_PRECISION)
                for region in regions
            })
        )
        if legacy_prices != region_prices:
            raise NodeMonitorTargetUnavailableError(
                reason="canonical_target_set_mismatch"
            )

        # §16-17, §23 按价格去重构建 targets（升序，deterministic）
        price_to_region_ids: dict[float, list[str]] = {}
        for region in regions:
            price = round(float(region["mid"]), _PRICE_PRECISION)
            price_to_region_ids.setdefault(price, []).append(region["entity_id"])

        # §19-21 Target Set version 绑定四类身份（完整 64 位 SHA256）
        version_payload = {
            "schema_version": _NODE_MONITOR_TARGET_SCHEMA_VERSION,
            "daily_source_hash": node_input.daily_source_hash,
            "m15_source_hash": node_input.m15_source_hash,
            "adjustment_context_hash": node_input.adjustment_context_hash,
            "factor_hash": factor_hash,
            "algorithm_version": profile.algorithm_version,
            "output_schema_version": profile.output_schema_version,
            "contract_fingerprint": profile.contract_fingerprint,
            "profile_hash": profile.profile_hash,
            "node_regions_hash": node_regions_hash,
            "target_prices": [f"{price:.{_PRICE_PRECISION}f}" for price in region_prices],
        }
        target_set_version = _stable_sha256(version_payload)

        # §20-21 updated_through：最后一根 completed 15m bar（诊断 watermark，不进 version）
        updated_through = pd.Timestamp(node_input.bars_15m.index[-1])

        # §22 version-scoped target_id（完整 64 位 SHA256）
        targets = tuple(
            NodeMonitorTarget(
                target_id=hashlib.sha256(
                    (
                        f"{target_set_version}"
                        f"|peak|"
                        f"{price:.{_PRICE_PRECISION}f}"
                    ).encode()
                ).hexdigest(),
                kind="peak",
                price=price,
                source_region_ids=tuple(sorted(price_to_region_ids[price])),
            )
            for price in region_prices
        )

        return NodeMonitorTargetSet(
            target_set_version=target_set_version,
            targets=targets,
            daily_source_hash=node_input.daily_source_hash,
            m15_source_hash=node_input.m15_source_hash,
            adjustment_context_hash=node_input.adjustment_context_hash,
            factor_hash=factor_hash,
            algorithm_version=profile.algorithm_version,
            output_schema_version=profile.output_schema_version,
            contract_fingerprint=profile.contract_fingerprint,
            profile_hash=profile.profile_hash,
            node_regions_hash=node_regions_hash,
            updated_through=updated_through,
            input_availability=node_input.availability,
            input_degraded_reason=node_input.degraded_reason,
        )
