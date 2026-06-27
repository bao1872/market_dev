"""指标参数基线一致性测试。

验证代码、manifest 中的参数与 app.constants.indicator_contract 基线一致。
任何不一致均导致测试失败。
"""
import inspect
from pathlib import Path

import pytest
import yaml

from app.constants import indicator_contract as IC

# manifest 文件目录（backend/app/strategy_assets/manifests/）
_MANIFESTS_DIR = Path(__file__).parent.parent / "app" / "strategy_assets" / "manifests"


# ===== Volume Profile 参数 =====


def test_unified_volume_profile_vp_lookback_matches_baseline():
    """VP_LOOKBACK 应与基线 NODE_CLUSTER_PRIMARY_BARS 一致。"""
    from app.strategy_assets.algorithms.features.unified_volume_profile import (
        VP_LOOKBACK,
    )

    assert VP_LOOKBACK == IC.NODE_CLUSTER_PRIMARY_BARS


# ===== monitor_batch_service 行情回看参数（旧值应已迁移至基线）=====


def test_monitor_batch_service_no_daily_lookback_370():
    """旧值 _DAILY_LOOKBACK_DAYS=370 应已迁移至基线，源码中不得残留。"""
    from app.services import monitor_batch_service

    source = inspect.getsource(monitor_batch_service)
    assert "_DAILY_LOOKBACK_DAYS = 370" not in source


def test_monitor_batch_service_no_15min_lookback_800():
    """旧值 _15MIN_LOOKBACK_DAYS=800 应已迁移至基线，源码中不得残留。"""
    from app.services import monitor_batch_service

    source = inspect.getsource(monitor_batch_service)
    assert "_15MIN_LOOKBACK_DAYS = 800" not in source


# ===== watchlist_monitor.yaml 事件 TTL =====


def test_watchlist_monitor_node_cluster_touch_ttl_matches_baseline():
    """node_cluster_touch 的 state_ttl_seconds 应与基线 NODE_CLUSTER_EVENT_TTL_SECONDS 一致。"""
    yaml_path = _MANIFESTS_DIR / "watchlist_monitor.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    event_types = {e["key"]: e for e in data["event_types"]}
    assert (
        event_types["node_cluster_touch"]["state_ttl_seconds"]
        == IC.NODE_CLUSTER_EVENT_TTL_SECONDS
    )


# ===== dsa_selector.yaml lookback =====


def test_dsa_selector_yaml_lookback_matches_baseline():
    """dsa_selector.yaml 中 algorithm.lookback 默认值应与基线 DSA_LOOKBACK 一致。"""
    yaml_path = _MANIFESTS_DIR / "dsa_selector.yaml"
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    params = {p["key"]: p for p in data["parameters"]}
    assert params["algorithm.lookback"]["default"] == IC.DSA_LOOKBACK


# ===== indicator_service 各周期根数 =====


def test_indicator_service_daily_bars_matches_baseline():
    """INDICATOR_BARS['1d'] 应与基线 IC.INDICATOR_BARS['1d'] 一致。"""
    from app.services.indicator_service import INDICATOR_BARS

    assert INDICATOR_BARS["1d"] == IC.INDICATOR_BARS["1d"]


def test_indicator_service_15min_bars_matches_baseline():
    """INDICATOR_BARS['15m'] 应与基线 IC.INDICATOR_BARS['15m'] 一致。"""
    from app.services.indicator_service import INDICATOR_BARS

    assert INDICATOR_BARS["15m"] == IC.INDICATOR_BARS["15m"]


# ===== dsa_selector.py 旧常量清理 =====


def test_dsa_selector_no_default_lookback_360():
    """旧值 DEFAULT_LOOKBACK=360 应已迁移至基线，源码中不得残留。"""
    from app.strategy.selectors import dsa_selector

    source = inspect.getsource(dsa_selector)
    assert "DEFAULT_LOOKBACK = 360" not in source


# ===== budget.py 旧 manifest 引用清理 =====


def test_budget_no_volume_node_monitor_yaml_reference():
    """budget.py 不得引用已废弃的 volume_node_monitor.yaml。"""
    from app.strategy import budget

    source = inspect.getsource(budget)
    assert "volume_node_monitor.yaml" not in source
