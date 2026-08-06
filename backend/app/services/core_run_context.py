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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

# ===== 版本常量（集中治理，避免各处字符串漂移）=====
DSA_ALGORITHM_VERSION = "dsa-v1"
SMC_ALGORITHM_VERSION = "smc-v1"
MOMENTUM_ALGORITHM_VERSION = "momentum-v1"
BOLLINGER_ALGORITHM_VERSION = "bollinger-v1"
SQZMOM_ALGORITHM_VERSION = "sqzmom-v1"
CORE_EXECUTION_CONTRACT_VERSION = "core-exec-v1"
# [CHANGE-20260806-005 / Phase 1 / PC-11] CoreComputationArtifact schema 版本（单一真源，
# 与 core_artifact_codec.encode/decode 的 schemaVersion 保持一致）。bump 需新增 decode 分支并校验。
CORE_ARTIFACT_SCHEMA_VERSION = 1

# ===== [Phase 1 / PC-10] SMC/Bollinger/SQZMOM/VolumeContext 冻结 effective config =====
# 当前这些算法无 released StrategyVersion，用与代码实现一致的冻结参数进入 parameter_hash。
# 一旦接入 released resolver，应替换为 manifest 完整参数并移除此处代码常量。
# （来源：first_pyramid_service._FIRST_PYRAMID_PARAMS 的快照，集中治理避免漂移。）
_FROZEN_SMC_CONFIG: dict[str, Any] = {
    "pine_mode": "smc-pine",
}
_FROZEN_BOLLINGER_CONFIG: dict[str, Any] = {
    "bb_win": 20,
    "bb_k": 2.0,
}
_FROZEN_SQZMOM_CONFIG: dict[str, Any] = {
    "length": 20,
    "mult": 2.0,
}
_FROZEN_VOLUME_CONTEXT_CONFIG: dict[str, Any] = {
    "short_ma": 20,
    "long_ma": 200,
}

# ===== compute-once 计数键（唯一 SSOT）=====
# [CHANGE-20260806-005 / Phase 1 / PC-02] 五类 kernel + canonical frame 独立计数。
# canonical_frame_build 代表 canonical(1d) 帧被消费计算一次；
# dsa / smc / bollinger / sqzmom / volume_context 为该 canonical 帧上各算法 kernel 调用次数。
# （PC-02：DSA、SMC、Bollinger、SQZMOM 和 VolumeContext 每股每 core run 各计算一次。）
_COMPUTE_ONCE_KEYS: tuple[str, ...] = (
    "canonical_frame_build",
    "dsa",
    "smc",
    "bollinger",
    "sqzmom",
    "volume_context",
)


class ComputeOnceGateError(RuntimeError):
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
    bollinger: int = 0
    sqzmom: int = 0
    volume_context: int = 0
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
                "bollinger": self.bollinger,
                "sqzmom": self.sqzmom,
                "volume_context": self.volume_context,
            }


def enforce_compute_once_gate(
    diagnostics: ComputeOnceDiagnostics,
    eligible_compute_count: int,
) -> None:
    """compute-once 硬门禁：五类 kernel + canonical frame 六类计数都必须等于 eligible_compute_count。

    任一不一致即抛 ComputeOnceGateError，调用方不得发布 stock_core。

    Args:
        diagnostics: run-scoped 计数
        eligible_compute_count: 本 run 实际纳入核心计算的标的数

    Raises:
        ComputeOnceGateError: 任一维度计数与 eligible 不一致
    """
    counts = diagnostics.to_dict()
    for key in _COMPUTE_ONCE_KEYS:
        actual = counts[key]
        if actual != eligible_compute_count:
            raise ComputeOnceGateError(
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
    # [CHANGE-20260806-005 / Phase 1 / PC-10] run 开始时冻结 run mode 与日线 cutoff：
    # 单股不得自行重新解析配置；source_cutoff 代表日线数据截止（= trade date 的 PIT 截止）。
    run_mode: str = field(default="after_close")
    source_cutoff: str | None = field(default=None)

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
    # [CHANGE-20260806-005 / Phase 1 / PC-11] artifact schema 版本：encode/decode 需无损
    # round-trip，schema 版本用于未来兼容演进。
    schema_version: int = field(default=CORE_ARTIFACT_SCHEMA_VERSION)

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
            "schema_version": self.schema_version,
        }


def make_parameter_hash(context: CoreRunContext) -> str:
    """便捷入口：返回 run context 的参数 hash。"""
    return context.parameter_hash


# ===========================================================================
# [CHANGE-20260805-CP4A-CP3 / P0-02] released-config 唯一来源
# ===========================================================================
# market-data contract 版本（canonical daily frame 的输入合同：adj + source bar hash）
MARKET_DATA_CONTRACT_VERSION = "mdc-v1"


class ReleasedConfigError(RuntimeError):
    """released config 解析失败（无 released version / manifest 缺失等）。"""


class ReleasedConfigResolver(Protocol):
    """released 配置解析抽象（便于 fake repository 单测，不连真实 DB）。"""

    async def resolve_released_dsa_config(
        self,
        *,
        trade_date: date,
    ) -> dict[str, Any]:
        """解析 released dsa_selector StrategyVersion。

        Returns:
            {"dsa_version": str, "dsa_effective_config": dict, "dsa_build_hash": str}

        Raises:
            ReleasedConfigError: 无 released version 或 manifest 缺 parameters
        """
        ...


class SqlAlchemyReleasedConfigResolver:
    """默认 SQLAlchemy 实现：查询 released dsa_selector StrategyVersion。

    使用 strategy_service.list_versions / release 语义；无 released version 时 fail-closed。
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def resolve_released_dsa_config(
        self,
        *,
        trade_date: date,
    ) -> dict[str, Any]:
        # 延迟 import 避免模块级 DB 依赖（纯模块可单测）
        from sqlalchemy import select

        from app.models.strategy import StrategyDefinition, StrategyVersion

        stmt = (
            select(StrategyVersion, StrategyDefinition.strategy_key)
            .join(
                StrategyDefinition,
                StrategyDefinition.id == StrategyVersion.strategy_definition_id,
            )
            .where(
                StrategyDefinition.strategy_key == "dsa_selector",
                StrategyVersion.status == "released",
            )
            .order_by(StrategyVersion.version.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        row = result.first()
        if row is None:
            raise ReleasedConfigError(
                "dsa_selector 无 released StrategyVersion（scheduled 模式禁止回退代码常量）"
            )
        version, _key = row
        manifest = version.manifest or {}
        parameters = manifest.get("parameters")
        if isinstance(parameters, dict):
            dsa_effective_config = dict(parameters)
        elif isinstance(parameters, list):
            # [CHANGE-20260806-005 / Phase 7] released dsa_selector manifest 的 parameters 是
            # **参数 spec 数组**（每项 {key, type, default, description, ...}，见 strategy_service
            # 的 manifest.get("parameters", [])），而非 {key: value} dict。DSASelector 以
            # params.get("atr_rope.length", 14) 等点分 key 消费，故把 spec 数组映射为
            # {key: default} 的 effective config 字典；缺 default 的项跳过。
            dsa_effective_config = {}
            for spec in parameters:
                if not isinstance(spec, dict) or "key" not in spec or "default" not in spec:
                    continue
                dsa_effective_config[str(spec["key"])] = spec["default"]
        else:
            raise ReleasedConfigError(
                "released dsa_selector StrategyVersion 的 manifest 缺 parameters"
            )
        return {
            "dsa_version": str(getattr(version, "version", "")),
            "dsa_build_hash": str(getattr(version, "build_hash", "")),
            "dsa_effective_config": dsa_effective_config,
        }


async def resolve_core_run_context(
    *,
    trade_date: date,
    snapshot_run_id: Any,
    eligible_instrument_ids: Sequence[Any],
    run_calculated_at: datetime | None = None,
    resolver: ReleasedConfigResolver | None = None,
    execution_contract_version: str = CORE_EXECUTION_CONTRACT_VERSION,
    run_mode: str = "after_close",
    source_cutoff: str | None = None,
    universe_version: str = "v1",
) -> CoreRunContext:
    """解析并冻结一次 scheduled stock_core run 的 run-level CoreRunContext。

    [CHANGE-20260805-CP4A-CP3 / P0-02] scheduled 模式的 config 唯一来源：
    - DSA：必须解析 released dsa_selector StrategyVersion；无 released 时 **fail-closed**，
      禁止回退代码常量。
    - SMC / momentum / volume：当前以冻结代码常量版本进入 config（与 DSA manifest 隔离），
      后续若这些算法也有 released StrategyVersion，接入同一 resolver 路径。
    - universe hash / market-data contract / adjustment contract 一并冻结。

    Args:
        trade_date: 交易日
        snapshot_run_id: StockFeatureSnapshotRun id
        eligible_instrument_ids: eligible universe（参与 core 计算的标的）
        run_calculated_at: run 级时钟（默认 now）
        resolver: released-config 解析器（默认 SqlAlchemyReleasedConfigResolver）
        execution_contract_version: 执行合同版本

    Returns:
        冻结的 CoreRunContext

    Raises:
        ReleasedConfigError: 无 released dsa_selector StrategyVersion（fail-closed）
    """
    if run_calculated_at is None:
        run_calculated_at = datetime.now(UTC)
    if resolver is None:
        raise ReleasedConfigError(
            "resolve_core_run_context 需要 released-config resolver（scheduled 模式禁止"
            "无 released 版本时回退代码常量）"
        )

    # 解析 released DSA（fail-closed）
    dsa_cfg = await resolver.resolve_released_dsa_config(trade_date=trade_date)

    # 冻结 universe hash（顺序无关）
    universe_hash = hashlib.sha256(
        "\x00".join(sorted(str(i) for i in eligible_instrument_ids)).encode()
    ).hexdigest()[:16]

    # 算法版本：DSA 用 released version；SMC/momentum/bollinger/sqzmom 冻结代码常量版本
    algorithm_versions = build_default_algorithm_versions()
    algorithm_versions["dsa"] = dsa_cfg["dsa_version"]

    # [CHANGE-20260806-005 / Phase 1 / PC-10] 完整 CoreRunContext 合同：除 DSA 外，
    # SMC/Bollinger/SQZMOM/VolumeContext 的 effective config 与 adjustment/market-data/
    # cutoff/hash 全部进入 config（从而进入 parameter_hash）。SMC/momentum/volume 当前
    # 无 released StrategyVersion，用与 algorithm_versions 对应的冻结配置（一旦这些算法
    # 建立 released version，接入 resolver 路径并移除代码常量回退）。
    # [CHANGE-20260806 / P0-A] 既有完整冻结：adjustment_as_of、universe hash/size、
    # market-data/adjustment 合同版本、source_bar_hash/adj_factor_hash 占位。
    config: dict[str, Any] = {
        "dsa": dsa_cfg["dsa_effective_config"],
        "smc": {
            "version": algorithm_versions.get("smc"),
            # SMC effective config：当前以冻结代码常量（完整参数在 _FIRST_PYRAMID_PARAMS 等）
            # 进入 parameter_hash；若后续接入 released resolver，此处替换为完整 manifest config。
            "effective_config": _FROZEN_SMC_CONFIG,
        },
        "momentum": {
            "version": algorithm_versions.get("momentum"),
            "bollinger_version": algorithm_versions.get("bollinger"),
            "sqzmom_version": algorithm_versions.get("sqzmom"),
            # [Phase 1 / PC-10] Bollinger / SQZMOM effective config 完整冻结（进入 hash）。
            "bollinger_effective_config": _FROZEN_BOLLINGER_CONFIG,
            "sqzmom_effective_config": _FROZEN_SQZMOM_CONFIG,
        },
        "volume_context": {
            "version": "vc-v1",
            # [Phase 1 / PC-10] VolumeContext effective config 完整冻结（进入 hash）。
            "effective_config": _FROZEN_VOLUME_CONTEXT_CONFIG,
        },
        "eligible_universe_hash": universe_hash,
        "eligible_universe_size": len(eligible_instrument_ids),
        "universe_version": universe_version,
        "market_data_contract_version": MARKET_DATA_CONTRACT_VERSION,
        "adjustment_contract_version": "adj-v1",
        # [P0-A] adjustment_as_of = run 交易日（复权基准日），作为合同一部分进入 hash
        "adjustment_as_of": trade_date.isoformat(),
        # [Phase 1 / PC-10] run mode 与日线 cutoff 作为合同一部分进入 hash
        "run_mode": run_mode,
        "source_cutoff": source_cutoff,
        "dsa_build_hash": dsa_cfg.get("dsa_build_hash"),
        # [P0-A] lineage 输入 hash 占位（由上游传入时覆盖；缺省为 "" 保持 hash 可复现）
        "source_bar_hash": "",
        "adj_factor_hash": "",
    }

    return CoreRunContext(
        trade_date=trade_date,
        run_calculated_at=run_calculated_at,
        algorithm_versions=algorithm_versions,
        config=config,
        execution_contract_version=execution_contract_version,
        run_id=snapshot_run_id,
        run_mode=run_mode,
        source_cutoff=source_cutoff,
    )


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
