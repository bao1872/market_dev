"""特征因果口径 registry。

定义生产 snapshot 与研究矩阵的字段命名空间与因果口径：
- causal.*: 当时可知的滚动特征（ATR/BB/SQZMOM/volume/active_swing/developing_swing/dsa_confirmed_*）
- confirmed_delay.*: 仅在确认 bar 生效的字段（confirmed_swing/bars_since_confirmed_swing）
- hindsight.*: 允许未来信息的结构标注（dsa_finalized_*/node_cluster_*），禁止进入回测
- label.*: 未来收益/胜负标签（future_return/future_max_drawdown/breakout_success），只能作为 y

设计原则：
- 生产 stock_feature_snapshots 只服务最近交易日 + 自选股 + 前端展示，必须 point-in-time
- 研究 feature matrix 用于探索因子组合规律，可同时包含 causal/hindsight/label，但严格分命名空间
- 禁止把 hindsight 或 label 字段当成 causal feature

用法：
    from app.research.feature_causality_registry import build_default_registry
    reg = build_default_registry()
    causal_specs = reg.by_namespace("causal")

模块自测：
    python -m app.research.feature_causality_registry
"""

from __future__ import annotations

from dataclasses import dataclass

# 命名空间常量
NS_CAUSAL = "causal"
NS_CONFIRMED_DELAY = "confirmed_delay"
NS_HINDSIGHT = "hindsight"
NS_LABEL = "label"

# compute_policy 常量
POLICY_SERIES_ONCE = "series_once"
POLICY_CONFIRMED_ONLY = "confirmed_only"
POLICY_HINDSIGHT_ONCE = "hindsight_once"
POLICY_FUTURE_LABEL = "future_label"


@dataclass(frozen=True)
class FeatureSpec:
    """单个特征字段的因果口径定义。

    Attributes:
        key: 字段唯一键，必须以 "{namespace}." 开头（如 "causal.atr"）
        namespace: 命名空间（causal / confirmed_delay / hindsight / label）
        source: 计算来源（如 structural_factor_service / dsa_selector）
        allowed_for_backtest: 是否允许作为回测 feature（hindsight/label 必须 False）
        compute_policy: 计算策略
            - series_once: 滚动序列特征，当时可知
            - confirmed_only: 仅在确认 bar 生效，不回填 anchor date
            - hindsight_once: 允许未来信息，只做结构标注
            - future_label: 未来标签，只能作为 y
        notes: 补充说明
    """

    key: str
    namespace: str
    source: str
    allowed_for_backtest: bool
    compute_policy: str
    notes: str = ""

    def __post_init__(self) -> None:
        """校验必填字段与 key 前缀匹配 namespace。"""
        if not self.namespace:
            raise ValueError("namespace 不能为空")
        if not self.source:
            raise ValueError("source 不能为空")
        if not self.compute_policy:
            raise ValueError("compute_policy 不能为空")
        prefix = f"{self.namespace}."
        if not self.key.startswith(prefix):
            raise ValueError(
                f"key prefix 必须匹配 namespace: key={self.key} 应以 '{prefix}' 开头"
            )

    @property
    def db_column(self) -> str:
        """将 dotted key 映射为 DB 下划线列名。

        causal.atr → causal_atr
        hindsight.dsa_finalized_segment → hindsight_dsa_finalized_segment
        label.future_return_10d → label_future_return_10d
        """
        return self.key.replace(".", "_")


class FeatureCausalityRegistry:
    """特征因果口径注册表。

    存储所有已登记的 FeatureSpec，提供按 key/namespace 查询。
    """

    def __init__(self) -> None:
        self._specs: dict[str, FeatureSpec] = {}

    def register(self, spec: FeatureSpec) -> None:
        """登记一个 FeatureSpec，重复 key 抛 ValueError。"""
        if spec.key in self._specs:
            raise ValueError(f"duplicate key: {spec.key}")
        self._specs[spec.key] = spec

    def get(self, key: str) -> FeatureSpec | None:
        """按 key 查找 FeatureSpec，不存在返回 None。"""
        return self._specs.get(key)

    def all(self) -> list[FeatureSpec]:
        """返回所有已登记的 FeatureSpec。"""
        return list(self._specs.values())

    def by_namespace(self, namespace: str) -> list[FeatureSpec]:
        """返回指定命名空间的所有 FeatureSpec。"""
        return [s for s in self._specs.values() if s.namespace == namespace]

    def keys(self) -> list[str]:
        """返回所有已登记的 key。"""
        return list(self._specs.keys())

    def db_columns(self) -> list[str]:
        """返回所有字段的 DB 下划线列名。"""
        return [spec.db_column for spec in self._specs.values()]


def build_default_registry() -> FeatureCausalityRegistry:
    """构建默认因果口径 registry，登记所有规范字段。

    字段分组：
    - causal: 当时可知的滚动特征（allowed_for_backtest=True, policy=series_once）
    - confirmed_delay: 确认 bar 生效的字段（allowed_for_backtest=True, policy=confirmed_only）
    - hindsight: 允许未来信息的结构标注（allowed_for_backtest=False, policy=hindsight_once）
    - label: 未来收益/胜负标签（allowed_for_backtest=False, policy=future_label）
    """
    reg = FeatureCausalityRegistry()

    # ===== causal: 当时可知的滚动特征（individual DB columns）=====
    causal_source = "structural_factor_service"
    causal_fields = [
        ("causal.atr", "ATR 波动率"),
        ("causal.bb_percent_b", "BB %B（close 在 band 中的位置）"),
        ("causal.bb_bandwidth_pct", "BB 带宽百分比"),
        ("causal.sqzmom_val", "SQZMOM 动量值"),
        ("causal.sqzmom_delta_1", "SQZMOM 一阶差分"),
        ("causal.volume_ratio_20", "20 日成交量比率"),
        ("causal.volume_percentile_120", "120 日成交量百分位"),
        ("causal.active_swing_dir", "active swing 方向（当时可知）"),
        ("causal.active_swing_high", "active swing 高点"),
        ("causal.active_swing_low", "active swing 低点"),
        ("causal.developing_swing_dir", "developing swing 方向"),
        ("causal.developing_swing_high", "developing swing 高点"),
        ("causal.developing_swing_low", "developing swing 低点"),
        ("causal.dsa_confirmed_segment", "DSA 段（当时已确认状态）"),
        ("causal.dsa_confirmed_direction", "DSA 方向（当时已确认）"),
        ("causal.dsa_confirmed_age_bars", "DSA 段已持续 bar 数（当时已确认）"),
    ]
    for key, note in causal_fields:
        reg.register(
            FeatureSpec(
                key=key,
                namespace=NS_CAUSAL,
                source=causal_source,
                allowed_for_backtest=True,
                compute_policy=POLICY_SERIES_ONCE,
                notes=note,
            )
        )

    # ===== confirmed_delay: 仅在确认 bar 生效的字段 =====
    cd_source = "structural_factor_service.swing"
    cd_fields = [
        ("confirmed_delay.confirmed_swing_high", "已确认 swing 高点 anchor"),
        ("confirmed_delay.confirmed_swing_low", "已确认 swing 低点 anchor"),
        (
            "confirmed_delay.bars_since_confirmed_swing_high",
            "距上次确认 swing 高点的 bar 数",
        ),
        (
            "confirmed_delay.bars_since_confirmed_swing_low",
            "距上次确认 swing 低点的 bar 数",
        ),
    ]
    for key, note in cd_fields:
        reg.register(
            FeatureSpec(
                key=key,
                namespace=NS_CONFIRMED_DELAY,
                source=cd_source,
                allowed_for_backtest=True,
                compute_policy=POLICY_CONFIRMED_ONLY,
                notes=note,
            )
        )

    # ===== hindsight: 允许未来信息的结构标注，禁止进入回测 =====
    hindsight_fields = [
        (
            "hindsight.dsa_finalized_segment",
            "DSA 段（未来确认后回标注）",
            "dsa_selector",
        ),
        (
            "hindsight.dsa_finalized_direction",
            "DSA 方向（未来确认后）",
            "dsa_selector",
        ),
        (
            "hindsight.dsa_finalized_age_bars",
            "DSA 段最终持续 bar 数",
            "dsa_selector",
        ),
        (
            "hindsight.node_cluster_label",
            "Node Cluster 结构标注（允许未来信息）",
            "volume_node_monitor",
        ),
        (
            "hindsight.node_cluster_support",
            "Node Cluster 支撑结构（后验标注）",
            "volume_node_monitor",
        ),
        (
            "hindsight.node_cluster_resistance",
            "Node Cluster 阻力结构（后验标注）",
            "volume_node_monitor",
        ),
    ]
    for key, note, source in hindsight_fields:
        reg.register(
            FeatureSpec(
                key=key,
                namespace=NS_HINDSIGHT,
                source=source,
                allowed_for_backtest=False,
                compute_policy=POLICY_HINDSIGHT_ONCE,
                notes=note,
            )
        )

    # ===== label: 未来收益/胜负标签，只能作为 y =====
    label_source = "research_label_service"
    label_fields = [
        ("label.future_return_5d", "未来 5 日收益率"),
        ("label.future_return_10d", "未来 10 日收益率"),
        ("label.future_return_20d", "未来 20 日收益率"),
        ("label.future_max_drawdown_10d", "未来 10 日最大回撤"),
        ("label.future_max_drawdown_20d", "未来 20 日最大回撤"),
        ("label.breakout_success_10d", "未来 10 日是否突破成功"),
        ("label.failure_breakdown_10d", "未来 10 日是否破位失败"),
    ]
    for key, note in label_fields:
        reg.register(
            FeatureSpec(
                key=key,
                namespace=NS_LABEL,
                source=label_source,
                allowed_for_backtest=False,
                compute_policy=POLICY_FUTURE_LABEL,
                notes=note,
            )
        )

    return reg


if __name__ == "__main__":
    """模块自测：打印 registry 摘要。"""
    reg = build_default_registry()
    print(f"已登记字段总数: {len(reg.all())}")
    for ns in [NS_CAUSAL, NS_CONFIRMED_DELAY, NS_HINDSIGHT, NS_LABEL]:
        specs = reg.by_namespace(ns)
        print(f"\n[{ns}] {len(specs)} 个字段")
        for s in specs:
            bt = "Y" if s.allowed_for_backtest else "N"
            print(f"  {s.key:50s} policy={s.compute_policy:20s} backtest={bt}")
