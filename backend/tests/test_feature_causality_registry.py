"""feature_causality_registry 测试。

验证因果口径 registry 的核心规则：
1. FeatureSpec 必须有 namespace/source/compute_policy
2. hindsight.allowed_for_backtest 必须 False
3. label.allowed_for_backtest 必须 False
4. causal.allowed_for_backtest 必须 True
5. key 必须带 namespace 前缀
6. DSA 必须同时存在 causal 和 hindsight 两类
7. Node Cluster 只能是 hindsight，不能是 causal
8. confirmed swing 必须是 confirmed_delay，不得作为 hindsight 默认回填

用法：
    cd backend && APP_ENV=test pytest tests/test_feature_causality_registry.py -v
"""

from __future__ import annotations

import pytest

from app.research.feature_causality_registry import (
    NS_CAUSAL,
    NS_CONFIRMED_DELAY,
    NS_HINDSIGHT,
    NS_LABEL,
    FeatureCausalityRegistry,
    FeatureSpec,
    build_default_registry,
)

# ===== 1. FeatureSpec 必填字段校验 =====


def test_feature_spec_requires_namespace() -> None:
    """FeatureSpec 缺少 namespace 应抛 ValueError。"""
    with pytest.raises(ValueError, match="namespace"):
        FeatureSpec(
            key="causal.atr",
            namespace="",
            source="structural_factor_service",
            allowed_for_backtest=True,
            compute_policy="series_once",
        )


def test_feature_spec_requires_source() -> None:
    """FeatureSpec 缺少 source 应抛 ValueError。"""
    with pytest.raises(ValueError, match="source"):
        FeatureSpec(
            key="causal.atr",
            namespace=NS_CAUSAL,
            source="",
            allowed_for_backtest=True,
            compute_policy="series_once",
        )


def test_feature_spec_requires_compute_policy() -> None:
    """FeatureSpec 缺少 compute_policy 应抛 ValueError。"""
    with pytest.raises(ValueError, match="compute_policy"):
        FeatureSpec(
            key="causal.atr",
            namespace=NS_CAUSAL,
            source="structural_factor_service",
            allowed_for_backtest=True,
            compute_policy="",
        )


# ===== 5. key 必须带 namespace 前缀 =====


def test_key_must_have_namespace_prefix() -> None:
    """key 前缀必须匹配 namespace，否则抛 ValueError。

    例如 namespace=causal 时 key 必须以 "causal." 开头。
    """
    with pytest.raises(ValueError, match="prefix"):
        FeatureSpec(
            key="hindsight.atr",
            namespace=NS_CAUSAL,
            source="structural_factor_service",
            allowed_for_backtest=True,
            compute_policy="series_once",
        )


def test_key_prefix_matches_namespace_ok() -> None:
    """key 前缀匹配 namespace 时正常创建。"""
    spec = FeatureSpec(
        key="causal.atr",
        namespace=NS_CAUSAL,
        source="structural_factor_service",
        allowed_for_backtest=True,
        compute_policy="series_once",
    )
    assert spec.key == "causal.atr"


# ===== 2/3/4. namespace 级别 allowed_for_backtest 规则 =====


def test_hindsight_not_allowed_for_backtest() -> None:
    """hindsight 命名空间的所有字段 allowed_for_backtest 必须 False。"""
    reg = build_default_registry()
    hindsight_specs = reg.by_namespace(NS_HINDSIGHT)
    assert len(hindsight_specs) > 0, "hindsight 命名空间不应为空"
    for spec in hindsight_specs:
        assert spec.allowed_for_backtest is False, (
            f"hindsight 字段 {spec.key} 不允许进入回测"
        )


def test_label_not_allowed_for_backtest() -> None:
    """label 命名空间的所有字段 allowed_for_backtest 必须 False。"""
    reg = build_default_registry()
    label_specs = reg.by_namespace(NS_LABEL)
    assert len(label_specs) > 0, "label 命名空间不应为空"
    for spec in label_specs:
        assert spec.allowed_for_backtest is False, (
            f"label 字段 {spec.key} 不允许作为 feature 进入回测"
        )


def test_causal_allowed_for_backtest() -> None:
    """causal 命名空间的所有字段 allowed_for_backtest 必须 True。"""
    reg = build_default_registry()
    causal_specs = reg.by_namespace(NS_CAUSAL)
    assert len(causal_specs) > 0, "causal 命名空间不应为空"
    for spec in causal_specs:
        assert spec.allowed_for_backtest is True, (
            f"causal 字段 {spec.key} 应允许回测"
        )


def test_confirmed_delay_allowed_for_backtest() -> None:
    """confirmed_delay 命名空间字段允许回测（仅在确认 bar 生效后）。"""
    reg = build_default_registry()
    cd_specs = reg.by_namespace(NS_CONFIRMED_DELAY)
    assert len(cd_specs) > 0, "confirmed_delay 命名空间不应为空"
    for spec in cd_specs:
        assert spec.allowed_for_backtest is True, (
            f"confirmed_delay 字段 {spec.key} 应允许回测（确认后生效）"
        )


# ===== 6. DSA 必须同时存在 causal 和 hindsight 两类 =====


def test_dsa_has_both_causal_and_hindsight() -> None:
    """DSA 必须同时登记 causal.dsa_confirmed_* 和 hindsight.dsa_finalized_*。"""
    reg = build_default_registry()
    causal_dsa = [
        s for s in reg.by_namespace(NS_CAUSAL) if "dsa" in s.key
    ]
    hindsight_dsa = [
        s for s in reg.by_namespace(NS_HINDSIGHT) if "dsa" in s.key
    ]
    assert len(causal_dsa) > 0, "必须登记 causal.dsa_confirmed_* 字段"
    assert len(hindsight_dsa) > 0, "必须登记 hindsight.dsa_finalized_* 字段"


def test_dsa_causal_uses_confirmed_policy() -> None:
    """causal.dsa_confirmed_* 的 compute_policy 必须是 series_once（当时可知）。"""
    reg = build_default_registry()
    causal_dsa = [
        s for s in reg.by_namespace(NS_CAUSAL) if "dsa" in s.key
    ]
    for spec in causal_dsa:
        assert spec.compute_policy == "series_once", (
            f"causal DSA {spec.key} compute_policy 应为 series_once"
        )


def test_dsa_hindsight_uses_hindsight_once_policy() -> None:
    """hindsight.dsa_finalized_* 的 compute_policy 必须是 hindsight_once。"""
    reg = build_default_registry()
    hindsight_dsa = [
        s for s in reg.by_namespace(NS_HINDSIGHT) if "dsa" in s.key
    ]
    for spec in hindsight_dsa:
        assert spec.compute_policy == "hindsight_once", (
            f"hindsight DSA {spec.key} compute_policy 应为 hindsight_once"
        )


# ===== 7. Node Cluster 只能是 hindsight，不能是 causal =====


def test_node_cluster_is_hindsight_only() -> None:
    """Node Cluster 字段必须只出现在 hindsight 命名空间。"""
    reg = build_default_registry()
    node_specs = [s for s in reg.all() if "node_cluster" in s.key]
    assert len(node_specs) > 0, "必须登记 hindsight.node_cluster_* 字段"
    for spec in node_specs:
        assert spec.namespace == NS_HINDSIGHT, (
            f"Node Cluster {spec.key} 必须属于 hindsight 命名空间"
        )


def test_node_cluster_not_in_causal() -> None:
    """causal 命名空间不得包含 node_cluster 字段。"""
    reg = build_default_registry()
    causal_node = [
        s for s in reg.by_namespace(NS_CAUSAL) if "node_cluster" in s.key
    ]
    assert len(causal_node) == 0, "Node Cluster 不得作为 causal 字段"


# ===== 8. confirmed swing 必须是 confirmed_delay =====


def test_confirmed_swing_is_confirmed_delay() -> None:
    """confirmed_swing 字段必须属于 confirmed_delay 命名空间。"""
    reg = build_default_registry()
    confirmed_swing_specs = [
        s for s in reg.all() if "swing" in s.key and "confirmed" in s.key
    ]
    assert len(confirmed_swing_specs) > 0, "必须登记 confirmed_delay.swing_* 字段"
    for spec in confirmed_swing_specs:
        assert spec.namespace == NS_CONFIRMED_DELAY, (
            f"confirmed swing {spec.key} 必须属于 confirmed_delay 命名空间"
        )


def test_confirmed_swing_uses_confirmed_only_policy() -> None:
    """confirmed_swing 的 compute_policy 必须是 confirmed_only。"""
    reg = build_default_registry()
    confirmed_swing_specs = [
        s for s in reg.all() if "swing" in s.key and "confirmed" in s.key
    ]
    for spec in confirmed_swing_specs:
        assert spec.compute_policy == "confirmed_only", (
            f"confirmed swing {spec.key} compute_policy 应为 confirmed_only"
        )


# ===== Registry 基础操作 =====


def test_registry_get_by_key() -> None:
    """registry.get(key) 返回对应的 FeatureSpec。"""
    reg = build_default_registry()
    spec = reg.get("causal.atr")
    assert spec is not None
    assert spec.namespace == NS_CAUSAL


def test_registry_get_unknown_key_returns_none() -> None:
    """registry.get(unknown) 返回 None。"""
    reg = build_default_registry()
    assert reg.get("nonexistent.key") is None


def test_registry_register_duplicate_raises() -> None:
    """重复注册同一 key 应抛 ValueError。"""
    reg = FeatureCausalityRegistry()
    spec = FeatureSpec(
        key="causal.test",
        namespace=NS_CAUSAL,
        source="test",
        allowed_for_backtest=True,
        compute_policy="series_once",
    )
    reg.register(spec)
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(spec)


def test_registry_all_returns_all_specs() -> None:
    """registry.all() 返回所有已注册的 FeatureSpec。"""
    reg = build_default_registry()
    all_specs = reg.all()
    assert len(all_specs) > 0
    keys = {s.key for s in all_specs}
    assert "causal.atr" in keys
    assert "hindsight.node_cluster" in keys or any(
        "node_cluster" in k for k in keys
    )


def test_registry_by_namespace_filters_correctly() -> None:
    """by_namespace 只返回对应命名空间的字段。"""
    reg = build_default_registry()
    causal = reg.by_namespace(NS_CAUSAL)
    for s in causal:
        assert s.namespace == NS_CAUSAL
    hindsight = reg.by_namespace(NS_HINDSIGHT)
    for s in hindsight:
        assert s.namespace == NS_HINDSIGHT


# ===== 默认 registry 完整性 =====


def test_default_registry_has_required_causal_features() -> None:
    """默认 registry 必须包含关键 causal 字段。"""
    reg = build_default_registry()
    keys = {s.key for s in reg.all()}
    required = {
        "causal.atr",
        "causal.bb",
        "causal.sqzmom",
        "causal.volume_ratio_20",
        "causal.volume_percentile_120",
        "causal.active_swing",
        "causal.developing_swing",
    }
    missing = required - keys
    assert not missing, f"缺少 causal 字段: {missing}"


def test_default_registry_has_required_labels() -> None:
    """默认 registry 必须包含关键 label 字段。"""
    reg = build_default_registry()
    keys = {s.key for s in reg.all()}
    required = {
        "label.future_return_5d",
        "label.future_return_10d",
        "label.future_return_20d",
        "label.future_max_drawdown_10d",
        "label.future_max_drawdown_20d",
        "label.breakout_success_10d",
        "label.failure_breakdown_10d",
    }
    missing = required - keys
    assert not missing, f"缺少 label 字段: {missing}"


def test_label_compute_policy_is_future_label() -> None:
    """label 字段的 compute_policy 必须是 future_label。"""
    reg = build_default_registry()
    for spec in reg.by_namespace(NS_LABEL):
        assert spec.compute_policy == "future_label", (
            f"label {spec.key} compute_policy 应为 future_label"
        )
