"""算法合同注册表 — 所有指标/因子/特征算法族的唯一注册真源。

本文件是 Section 2 (CHANGE-20260718-006) 的核心基础设施：把 Node Cluster 三层
合同架构（constants + semantics + engine）推广为全部算法族的统一注册表与网关。

架构：
    MDAS 统一行情输入
            ↓
    Calculation Contract Registry（本文件）
            ↓
    Canonical Computation Service（app.services.canonical_computation_service）
            ↓
    各算法族唯一纯计算 Kernel（已存在的 engine/selector/monitor）
            ↓
    详情 / 盘后 / 盘中 / Capture 适配层

每个算法必须声明（AlgorithmContract）：
- algorithm_id: 算法唯一标识（如 "node_cluster"、"dsa"、"smc"）
- algorithm_version: 算法版本（语义变更时 bump，自动失效缓存）
- kernel_module: 计算内核所在模块路径
- kernel_entrypoint: 计算内核入口（module:callable 形式）
- input_timeframes: 输入周期（如 ("1d", "15m")）
- adjustment_mode: 复权方式（"qfq" | "none"）
- completed_only: 是否只用已完成 bar
- warmup_bars: 预热所需根数（用于 MDAS limit）
- output_schema_version: 输出 schema 版本
- contract_fingerprint: 合同指纹（语义变更时 bump，缓存键组成部分）

四条调用链（详情 / 盘后 / 盘中 / Capture）只能通过 CanonicalComputationService
调用已注册算法；禁止直接 import 算法 kernel 绕过注册表。

相同输入必须得到相同输出：
    instrument + timeframe + as_of + source_bar_hash + adj_factor_hash
        → contract_fingerprint + result_hash

用法：
    from app.contracts.algorithm_registry import AlgorithmRegistry, AlgorithmContract
    contract = AlgorithmRegistry.get("node_cluster")
    # 或通过 CanonicalComputationService.compute("node_cluster", ...)

注册新算法：
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="my_algo",
        algorithm_version="my-v1",
        kernel_module="app.services.my_algo_engine",
        kernel_entrypoint="app.services.my_algo_engine:compute_my_algo",
        ...
    ))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("contracts.algorithm_registry")

# Registry 版本：注册表结构变更时 bump（不影响各算法自身版本）
ALGORITHM_REGISTRY_VERSION: Final[str] = "reg-v1"


# =============================================================================
# 不可变合同数据类
# =============================================================================


@dataclass(frozen=True)
class AlgorithmContract:
    """单个算法族的合同（不可变）。

    Attributes:
        algorithm_id: 算法唯一标识（小写 snake_case，如 "node_cluster"）
        algorithm_version: 算法版本（语义变更时 bump）
        kernel_module: 计算内核所在模块路径（如 "app.services.node_cluster_engine"）
        kernel_entrypoint: 计算内核入口 callable（"module:function" 形式）
        input_timeframes: 输入周期元组（如 ("1d", "15m")）
        adjustment_mode: 复权方式（"qfq" 前复权 | "none" 不复权）
        completed_only: 是否只用已完成 bar（True=禁止 partial bar 进入计算）
        warmup_bars: 预热所需根数（用于 MDAS limit，0=无特殊要求）
        output_schema_version: 输出 schema 版本（DTO 字段变更时 bump）
        contract_fingerprint: 合同指纹（语义变更时 bump，缓存键组成部分）
        migration_status: 迁移状态（CHANGE-20260718-007 S3.2）
            "registered_only"=合同已登记但 kernel_entrypoint callable 不存在或未适配统一签名（默认，安全假设未接线）
            "input_provider_wired"=callable 存在且接受统一 (bars: pd.DataFrame, **kwargs) 签名，可经 compute_with_mdas 调用
            "production_wired"=callable 存在且为生产入口 adapter（CHANGE-20260719-001 §二），
                adapter 已在 canonical_adapters.py 中实现并通过自测。
                与 input_provider_wired 语义等价（compute_with_mdas 接受），
                但更强地断言"该算法已是生产 canonical 入口"，禁止再退回 registered_only。
        description: 算法描述（人类可读）
    """

    algorithm_id: str
    algorithm_version: str
    kernel_module: str
    kernel_entrypoint: str
    input_timeframes: tuple[str, ...]
    adjustment_mode: str  # "qfq" | "none"
    completed_only: bool
    warmup_bars: int
    output_schema_version: int
    contract_fingerprint: str
    migration_status: str = "registered_only"
    description: str = ""

    def __post_init__(self) -> None:
        """校验合同字段约束。"""
        if not self.algorithm_id or not self.algorithm_id.islower():
            raise ValueError(
                f"algorithm_id 必须为非空小写 snake_case: {self.algorithm_id!r}"
            )
        if not self.algorithm_version:
            raise ValueError("algorithm_version 不能为空")
        if ":" not in self.kernel_entrypoint:
            raise ValueError(
                f"kernel_entrypoint 必须为 module:function 形式: {self.kernel_entrypoint!r}"
            )
        if not self.kernel_module:
            raise ValueError("kernel_module 不能为空")
        if self.adjustment_mode not in ("qfq", "none"):
            raise ValueError(
                f"adjustment_mode 必须为 qfq 或 none: {self.adjustment_mode!r}"
            )
        if self.warmup_bars < 0:
            raise ValueError(f"warmup_bars 不能为负: {self.warmup_bars}")
        if self.output_schema_version < 1:
            raise ValueError(
                f"output_schema_version 必须 >= 1: {self.output_schema_version}"
            )
        if not self.contract_fingerprint:
            raise ValueError("contract_fingerprint 不能为空")
        if self.migration_status not in (
            "registered_only",
            "input_provider_wired",
            "production_wired",
        ):
            raise ValueError(
                f"migration_status 必须为 'registered_only' / 'input_provider_wired' / "
                f"'production_wired': {self.migration_status!r}"
            )
        # kernel_module 必须是 kernel_entrypoint 的前缀
        entrypoint_module = self.kernel_entrypoint.split(":", 1)[0]
        if entrypoint_module != self.kernel_module:
            raise ValueError(
                f"kernel_entrypoint 的 module 部分 ({entrypoint_module!r}) "
                f"必须等于 kernel_module ({self.kernel_module!r})"
            )


# =============================================================================
# 注册表（进程内单例）
# =============================================================================


class AlgorithmRegistry:
    """算法合同注册表（进程内单例）。

    所有算法族必须在启动时注册到此表；四条调用链通过 CanonicalComputationService
    查询此表获取合同后调度到对应 kernel。

    线程安全：注册表在应用启动时一次性注册，运行时只读，无需加锁。
    """

    _contracts: dict[str, AlgorithmContract] = {}

    @classmethod
    def register(cls, contract: AlgorithmContract) -> None:
        """注册算法合同。重复注册同 algorithm_id 视为编程错误。

        Args:
            contract: 算法合同实例

        Raises:
            ValueError: algorithm_id 已注册
        """
        if contract.algorithm_id in cls._contracts:
            existing = cls._contracts[contract.algorithm_id]
            if existing == contract:
                logger.debug(
                    "算法合同幂等注册 algorithm_id=%s version=%s",
                    contract.algorithm_id, contract.algorithm_version,
                )
                return
            raise ValueError(
                f"算法已注册且合同不同 algorithm_id={contract.algorithm_id} "
                f"existing_version={existing.algorithm_version} "
                f"new_version={contract.algorithm_version}"
            )
        cls._contracts[contract.algorithm_id] = contract
        logger.info(
            "算法合同已注册 algorithm_id=%s version=%s kernel=%s fingerprint=%s",
            contract.algorithm_id, contract.algorithm_version,
            contract.kernel_entrypoint, contract.contract_fingerprint,
        )

    @classmethod
    def get(cls, algorithm_id: str) -> AlgorithmContract:
        """获取算法合同。

        Args:
            algorithm_id: 算法唯一标识

        Returns:
            AlgorithmContract 实例

        Raises:
            KeyError: algorithm_id 未注册
        """
        if algorithm_id not in cls._contracts:
            raise KeyError(
                f"算法未注册: algorithm_id={algorithm_id} "
                f"已注册={list(cls._contracts.keys())}"
            )
        return cls._contracts[algorithm_id]

    @classmethod
    def list_all(cls) -> list[AlgorithmContract]:
        """返回所有已注册算法合同（按 algorithm_id 排序）。"""
        return sorted(cls._contracts.values(), key=lambda c: c.algorithm_id)

    @classmethod
    def list_ids(cls) -> list[str]:
        """返回所有已注册 algorithm_id（排序）。"""
        return sorted(cls._contracts.keys())

    @classmethod
    def is_registered(cls, algorithm_id: str) -> bool:
        """检查算法是否已注册。"""
        return algorithm_id in cls._contracts

    @classmethod
    def _reset_for_test(cls) -> None:
        """测试辅助：清空注册表（仅测试使用）。"""
        cls._contracts.clear()


# =============================================================================
# 预注册全部算法族（启动时一次性注册）
# =============================================================================


def _register_builtin_algorithms() -> None:
    """注册内置算法族合同。

    每个算法族对应一个唯一 kernel（已存在的 engine/selector/monitor）。
    新增算法族时在此函数末尾追加 AlgorithmRegistry.register(...)。
    """
    # ----- Node Cluster / Volume Profile -----
    # adapter: app.services.canonical_adapters.compute_node_cluster_adapter（多 timeframe）
    # 三链（盘后/详情/监控）统一入口，三层合同（constants+semantics+engine）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="node_cluster",
        algorithm_version="nc-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_node_cluster_adapter",
        input_timeframes=("1d", "15m"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="nc-cf-v1",
        migration_status="production_wired",
        description=(
            "Node Cluster / Volume Profile：1d 250 根已完成 qfq 决定价格范围，"
            "15m 4000 根已完成 qfq 分配成交量，1m 2 根只用于穿越检测。"
            "三链同 stock/as_of/输入 → profile_hash 必须一致。"
            "adapter 接受 daily_bars + bars_15m（多 timeframe，调用方用 compute() 直接调度）。"
        ),
    ))

    # ----- DSA (Dynamic Swing Algorithm) -----
    # adapter: app.services.canonical_adapters.compute_dsa_adapter
    # 包装 dsa_selector.compute_dsa_bundle（VWAP 计算 + 后处理 + metrics 提取）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="dsa",
        algorithm_version="dsa-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_dsa_adapter",
        input_timeframes=("1d",),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="dsa-cf-v1",
        migration_status="production_wired",
        description=(
            "DSA（Dynamic Swing Algorithm）：日线 250 根 qfq，"
            "输出 24 因子状态 + 事件检测。adapter 包装 compute_dsa_bundle，"
            "内部委托 DSASelector 的 calculate_state/detect_events。"
        ),
    ))

    # ----- SMC (Smart Money Concepts) -----
    # adapter: app.services.canonical_adapters.compute_smc_adapter
    # 包装 smc_indicator.compute_smc_indicators + smc_view_adapter.adapt_smc_to_display_dto
    # FVG 完全排除（不计算/不返回/不缓存/不渲染）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="smc",
        algorithm_version="smc-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_smc_adapter",
        input_timeframes=("1d",),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=500,
        output_schema_version=1,
        contract_fingerprint="smc-cf-v1",
        migration_status="production_wired",
        description=(
            "SMC（Smart Money Concepts）：日线 qfq，按需计算（include_smc=true）。"
            "参数：Historical=true, Internal=true, All=true, Swing length=50, "
            "Internal OB count=5, EQH/EQL confirmation=3 threshold=0.1。"
            "FVG 完全排除。adapter 二合一：compute_smc_indicators + adapt_smc_to_display_dto。"
        ),
    ))

    # ----- Bollinger Bands -----
    # adapter: app.services.canonical_adapters.compute_bollinger_adapter
    # 包装 bollinger_features_plotly.bollinger(df, win, k)
    # 参数：BB_WIN=20, BB_K=2.0
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="bollinger",
        algorithm_version="bb-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_bollinger_adapter",
        input_timeframes=("1d", "15m", "1h", "1w", "1mo"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="bb-cf-v1",
        migration_status="production_wired",
        description=(
            "Bollinger Bands：BB_WIN=20, BB_K=2.0，多周期支持。"
            "输出 bb_upper/bb_mid/bb_lower + 位置 0-1。"
        ),
    ))

    # ----- MACD -----
    # adapter: app.services.canonical_adapters.compute_macd_adapter（统一签名，S3.2 接线）
    # 包装 indicator_service.compute_macd（A 股 2× 版本）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="macd",
        algorithm_version="macd-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_macd_adapter",
        input_timeframes=("1d", "15m", "1h", "1w", "1mo"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="macd-cf-v1",
        migration_status="production_wired",
        description=(
            "MACD：标准 12/26/9 参数，输出 macd_line/signal_line/histogram。"
            "多周期支持。adapter 已接线（compute_macd_adapter），可经 compute_with_mdas 调用。"
        ),
    ))

    # ----- SQZMOM (Squeeze Momentum) -----
    # adapter: app.services.canonical_adapters.compute_sqzmom_adapter
    # 包装 sqzmom_lb.compute_sqzmom_lb（LazyBear Pine 复刻）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="sqzmom",
        algorithm_version="sqzmom-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_sqzmom_adapter",
        input_timeframes=("1d",),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="sqzmom-cf-v1",
        migration_status="production_wired",
        description=(
            "SQZMOM（Squeeze Momentum）：BB/Keltner 通道挤压 + 动量直方图。"
            "输出 squeeze_on/squeeze_off/momentum + 历史信号。"
        ),
    ))

    # ----- Breakout -----
    # adapter: app.services.canonical_adapters.compute_breakout_adapter
    # 包装 trendlines_with_breaks_luxalgo.trendlines_with_breaks(df, cfg)
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="breakout",
        algorithm_version="brk-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_breakout_adapter",
        input_timeframes=("1d",),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="brk-cf-v1",
        migration_status="production_wired",
        description=(
            "Breakout：趋势线突破检测，输出支撑/阻力线 + 突破信号。"
        ),
    ))

    # ----- Participation -----
    # adapter: app.services.canonical_adapters.compute_participation_adapter
    # 包装 sr_event_factor_lab.compute_sr_factor_lab(df, cfg)
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="participation",
        algorithm_version="part-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_participation_adapter",
        input_timeframes=("1d",),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="part-cf-v1",
        migration_status="production_wired",
        description=(
            "Participation：参与度因子，基于成交量分布与价格行为。"
        ),
    ))

    # ----- Temporal Features -----
    # adapter: app.services.canonical_adapters.compute_temporal_features_adapter（异步编排）
    # 包装 temporal_feature_service.compute_temporal_features（内部自行调 MDAS）
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="temporal_features",
        algorithm_version="tmp-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_temporal_features_adapter",
        input_timeframes=("1d", "15m"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="tmp-cf-v1",
        migration_status="production_wired",
        description=(
            "Temporal Features：时间特征因子（双周期 daily_context/m15_response/derived_relation）。"
            "异步编排 adapter，调用方用 compute() 直接调度（不能用 compute_with_mdas）。"
        ),
    ))

    # ----- Structural Features -----
    # adapter: app.services.canonical_adapters.compute_structural_features_adapter
    # 包装 structural_factor_service._compute_all_factors_for_bars
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="structural_features",
        algorithm_version="str-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_structural_features_adapter",
        input_timeframes=("1d", "15m"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="str-cf-v1",
        migration_status="production_wired",
        description=(
            "Structural Features：结构因子（dsa_segment/swing_position/cost_position/"
            "volatility_momentum/participation，含 Node Cluster profile 注入）。"
            "盘后 primary / 详情 / 监控三链经统一 adapter 入口。"
        ),
    ))

    # ----- Primary/Secondary Relation -----
    # adapter: app.services.canonical_adapters.compute_primary_secondary_relation_adapter
    # 包装 structural_factor_service._compute_relation(primary, secondary)
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="primary_secondary_relation",
        algorithm_version="psr-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_primary_secondary_relation_adapter",
        input_timeframes=("1d", "15m"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=1,
        contract_fingerprint="psr-cf-v1",
        migration_status="production_wired",
        description=(
            "Primary/Secondary Relation：主次周期对比关系（V1.8 客观关系字段）。"
            "输入 primary_factors + secondary_factors，输出 trend_alignment/position_delta 等。"
        ),
    ))

    # ----- Snapshot Derived Features -----
    # adapter: app.services.canonical_adapters.compute_snapshot_derived_adapter（异步编排）
    # 包装 feature_snapshot_service.compute_feature_snapshot_for_date
    AlgorithmRegistry.register(AlgorithmContract(
        algorithm_id="snapshot_derived_features",
        algorithm_version="sdf-v1",
        kernel_module="app.services.canonical_adapters",
        kernel_entrypoint="app.services.canonical_adapters:compute_snapshot_derived_adapter",
        input_timeframes=("1d", "15m"),
        adjustment_mode="qfq",
        completed_only=True,
        warmup_bars=250,
        output_schema_version=2,
        contract_fingerprint="sdf-cf-v1",
        migration_status="production_wired",
        description=(
            "Snapshot Derived Features：盘后快照衍生特征（MACD state + BB 位置 + "
            "Node Cluster profile + structural/temporal 因子聚合）。"
            "schema_version=2 含 source_bar_hash/adj_factor_hash/contract_version。"
            "异步编排 adapter，调用方用 compute() 直接调度。"
        ),
    ))


# 启动时一次性注册全部内置算法
_register_builtin_algorithms()


# =============================================================================
# 自测入口
# =============================================================================


def all_contracts() -> dict[str, dict[str, object]]:
    """返回所有算法合同的字典视图，供文档生成与一致性测试使用。"""
    return {
        c.algorithm_id: {
            "algorithm_version": c.algorithm_version,
            "kernel_entrypoint": c.kernel_entrypoint,
            "input_timeframes": list(c.input_timeframes),
            "adjustment_mode": c.adjustment_mode,
            "completed_only": c.completed_only,
            "warmup_bars": c.warmup_bars,
            "output_schema_version": c.output_schema_version,
            "contract_fingerprint": c.contract_fingerprint,
            "migration_status": c.migration_status,
            "description": c.description,
        }
        for c in AlgorithmRegistry.list_all()
    }


if __name__ == "__main__":
    print("=" * 60)
    print(f"算法合同注册表 (algorithm_registry.py) version={ALGORITHM_REGISTRY_VERSION}")
    print("=" * 60)
    contracts = all_contracts()
    for aid, c in contracts.items():
        print(f"\n[{aid}]")
        for k, v in c.items():
            print(f"  {k} = {v!r}")
    print("=" * 60)
    print(f"共 {len(contracts)} 个算法族已注册")
    print(f"algorithm_ids = {AlgorithmRegistry.list_ids()}")

    # 验证注册表完整性
    assert len(contracts) >= 12, f"应至少注册 12 个算法族，实际 {len(contracts)}"
    assert "node_cluster" in contracts
    assert "dsa" in contracts
    assert "smc" in contracts
    assert "bollinger" in contracts
    assert "macd" in contracts
    print("OK")
