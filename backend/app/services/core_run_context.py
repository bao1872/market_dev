"""CoreRunContext - 统一不可变 run context 与 DSA 计算产物容器。

[PRD V2.1 §6 / next.md EPIC-03]
- CoreRunContext 封装一次 stock_core run 的所有不可变输入：
  trade_date / run_calculated_at / universe / DSA·SMC·momentum 版本与配置 /
  参数 hash / 执行合同。
- CoreComputationArtifact 封装单股 core 计算结果：
  FirstPyramidCore / DSA projection payload / visual / events /
  availability / hashes / diagnostics。
- 目的是让 DSA/SMC/Bollinger/SQZMOM/VolumeContext 共享同一 canonical frame，
  保证 DSA 每股只计算一次（compute-once）。

本模块为纯数据结构 + 配置解析，不连接数据库，可纯单元测试。
实际逐股计算编排在 feature_snapshot_service 中消费本 context。

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.core_run_context
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# ===== 版本常量（集中治理，避免各处字符串漂移）=====
DSA_ALGORITHM_VERSION = "dsa-v1"
SMC_ALGORITHM_VERSION = "smc-v1"
MOMENTUM_ALGORITHM_VERSION = "momentum-v1"
BOLLINGER_ALGORITHM_VERSION = "bollinger-v1"
SQZMOM_ALGORITHM_VERSION = "sqzmom-v1"
CORE_EXECUTION_CONTRACT_VERSION = "core-exec-v1"

# ===== compute-once 计数键（唯一 SSOT）=====
# canonical_frame_build 代表 canonical(1d) 帧被消费计算一次；
# dsa / smc / momentum 为该 canonical 帧上各维度计算调用次数。
_COMPUTE_ONCE_KEYS: tuple[str, ...] = (
    "canonical_frame_build",
    "dsa",
    "smc",
    "momentum",
)


class ComputeOnceGateViolation(RuntimeError):
    """compute-once 硬门禁失败：维度调用次数 != 实际纳入计算标的数。

    触发时禁止发布 stock_core。
    """


class CoreArtifactLineageError(ValueError):
    """CoreComputationArtifact 缺少必需 lineage 字段。"""


@dataclass
class ComputeOnceDiagnostics:
    """Run-scoped compute-once 诊断计数（替代模块级计数器）。

    [Corrective-2 2026-08-05] 每个 CoreRunContext 持有一个独立实例，天然支持
    并发 run 隔离——两个 run 的计数互不污染。计数由计算链显式传递的
    diagnostics 实例累计（不再使用模块级全局计数）。
    """

    canonical_frame_build: int = 0
    dsa: int = 0
    smc: int = 0
    momentum: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bump(self, key: str) -> None:
        """对指定计算类型计数自增（线程安全）。"""
        if key not in _COMPUTE_ONCE_KEYS:
            raise ValueError(f"无效 compute-once 计数键: {key}")
        with self._lock:
            setattr(self, key, getattr(self, key) + 1)

    def to_dict(self) -> dict[str, int]:
        """转换为计数字典快照（线程安全）。"""
        with self._lock:
            return {
                "canonical_frame_build": self.canonical_frame_build,
                "dsa": self.dsa,
                "smc": self.smc,
                "momentum": self.momentum,
            }


def enforce_compute_once_gate(
    diagnostics: ComputeOnceDiagnostics,
    eligible_compute_count: int,
) -> None:
    """compute-once 硬门禁：四类计数都必须等于 eligible_compute_count。

    任一不一致即抛 ComputeOnceGateViolation，调用方不得发布 stock_core。

    Args:
        diagnostics: run-scoped 计数
        eligible_compute_count: 本 run 实际纳入核心计算的标的数

    Raises:
        ComputeOnceGateViolation: 任一维度计数与 eligible 不一致
    """
    counts = diagnostics.to_dict()
    for key in _COMPUTE_ONCE_KEYS:
        actual = counts[key]
        if actual != eligible_compute_count:
            raise ComputeOnceGateViolation(
                f"compute-once 门禁失败: {key}={actual} != eligible_compute_count="
                f"{eligible_compute_count}，禁止发布 stock_core"
            )


@dataclass(frozen=True)
class CoreRunContext:
    """一次 stock_core run 的不可变上下文。

    所有字段在 run 启动时冻结，后续计算不得修改。
    parameter_hash 由关键配置派生，用于跨 run 幂等与审计。
    compute_diagnostics 为 run-scoped compute-once 计数（并发隔离）。
    """

    trade_date: date
    run_calculated_at: datetime
    algorithm_versions: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    execution_contract_version: str = CORE_EXECUTION_CONTRACT_VERSION
    # [Corrective-2 2026-08-05] 绑定真实 run：计数与 lineage 对账必须归属同一 run
    run_id: Any | None = field(default=None)
    compute_diagnostics: ComputeOnceDiagnostics = field(
        default_factory=ComputeOnceDiagnostics
    )

    @property
    def parameter_hash(self) -> str:
        """由算法版本 + 配置派生稳定 hash（顺序无关）。"""
        material = {
            "algorithm_versions": dict(sorted(self.algorithm_versions.items())),
            "config": self._canonical_config(self.config),
            "execution_contract_version": self.execution_contract_version,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    @staticmethod
    def _canonical_config(config: dict[str, Any]) -> dict[str, Any]:
        """递归规范化为可排序结构。"""
        if not isinstance(config, dict):
            return config
        return {
            str(k): CoreRunContext._canonical_config(v)
            for k, v in sorted(config.items())
        }


def build_default_algorithm_versions() -> dict[str, str]:
    """默认算法版本集合（无已发布 StrategyVersion 时的冻结值）。"""
    return {
        "dsa": DSA_ALGORITHM_VERSION,
        "smc": SMC_ALGORITHM_VERSION,
        "momentum": MOMENTUM_ALGORITHM_VERSION,
        "bollinger": BOLLINGER_ALGORITHM_VERSION,
        "sqzmom": SQZMOM_ALGORITHM_VERSION,
    }


@dataclass
class CoreComputationArtifact:
    """单股 core 计算结果容器。

    - payload: FirstPyramidCore 结构化输出（含 DSA projection）
    - visual: 可视化/渲染用数据
    - events: 结构事件（结构完成/结构破坏等）
    - availability: 各维度可用性（structure/smc/momentum/...）
    - hashes: 输入/输出 hash（lineage 审计）
    - diagnostics: 诊断信息（调用次数、性能等）
    """

    instrument_id: Any
    trade_date: date
    payload: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    availability: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # [Commit C 修正 2026-08-05] 持久化 lineage：DSA projection 必须从 artifact 自身读取
    # source_core_run_id / parameter_hash / algorithm_versions，禁止消费端比较任意传入字符串。
    source_core_run_id: Any | None = field(default=None)
    parameter_hash: str = field(default="")
    algorithm_versions: dict[str, str] = field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        """所有必需核心维度均可用。

        [P0-8 修正] 正式第一金字塔 core 维度为 trend / structure / momentum。
        原实现误用 structure / smc / momentum（smc 与 structure 重复且未检查 DSA/trend）。
        """
        required = {"trend", "structure", "momentum"}
        for dim in required:
            if self.availability.get(dim) != "ready":
                return False
        return True

    def validate_lineage(self) -> None:
        """强制校验 lineage 必需字段（[Corrective-2 2026-08-05]）。

        - source_core_run_id 非空
        - parameter_hash 非空
        - dsa algorithm version 非空

        Raises:
            CoreArtifactLineageError: 任一必需 lineage 字段缺失
        """
        if self.source_core_run_id is None:
            raise CoreArtifactLineageError(
                "CoreComputationArtifact.source_core_run_id 不能为空"
            )
        if not self.parameter_hash:
            raise CoreArtifactLineageError(
                "CoreComputationArtifact.parameter_hash 不能为空"
            )
        if not self.algorithm_versions.get("dsa"):
            raise CoreArtifactLineageError(
                "CoreComputationArtifact 必须携带 dsa algorithm version"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": str(self.instrument_id),
            "trade_date": self.trade_date.isoformat(),
            "payload": self.payload,
            "visual": self.visual,
            "events": self.events,
            "availability": self.availability,
            "hashes": self.hashes,
            "diagnostics": self.diagnostics,
            "source_core_run_id": str(self.source_core_run_id) if self.source_core_run_id is not None else None,
            "parameter_hash": self.parameter_hash,
            "algorithm_versions": self.algorithm_versions,
        }


def make_parameter_hash(context: CoreRunContext) -> str:
    """便捷入口：返回 run context 的参数 hash。"""
    return context.parameter_hash


if __name__ == "__main__":
    ctx = CoreRunContext(
        trade_date=date(2026, 8, 4),
        run_calculated_at=datetime(2026, 8, 4, 15, 0, 0),
        algorithm_versions=build_default_algorithm_versions(),
        config={"bollinger": {"window": 20}},
    )
    ph1 = ctx.parameter_hash
    ctx2 = CoreRunContext(
        trade_date=date(2026, 8, 4),
        run_calculated_at=datetime(2026, 8, 4, 15, 0, 0),
        algorithm_versions=build_default_algorithm_versions(),
        config={"bollinger": {"window": 20}},
    )
    assert ph1 == ctx2.parameter_hash, "相同配置 hash 应一致"
    print(f"OK: CoreRunContext parameter_hash={ph1[:16]}...")
    artifact = CoreComputationArtifact(
        instrument_id="600000",
        trade_date=date(2026, 8, 4),
        availability={"trend": "ready", "structure": "ready", "momentum": "ready"},
    )
    assert artifact.is_available
    print("OK: CoreComputationArtifact availability verified")
