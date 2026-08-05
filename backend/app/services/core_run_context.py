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


@dataclass(frozen=True)
class CoreRunContext:
    """一次 stock_core run 的不可变上下文。

    所有字段在 run 启动时冻结，后续计算不得修改。
    parameter_hash 由关键配置派生，用于跨 run 幂等与审计。
    """

    trade_date: date
    run_calculated_at: datetime
    algorithm_versions: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    execution_contract_version: str = CORE_EXECUTION_CONTRACT_VERSION

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

    @property
    def is_available(self) -> bool:
        """所有必需维度均可用。"""
        required = {"structure", "smc", "momentum"}
        for dim in required:
            if self.availability.get(dim) != "ready":
                return False
        return True

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
        availability={"structure": "ready", "smc": "ready", "momentum": "ready"},
    )
    assert artifact.is_available
    print("OK: CoreComputationArtifact availability verified")
