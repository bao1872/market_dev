"""[V2.1 EPIC-03] CoreRunContext 纯数据结构单元测试。

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_core_run_context.py -q -p no:cacheprovider
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.core_run_context import (
    CoreComputationArtifact,
    CoreRunContext,
    build_default_algorithm_versions,
)


def _make_context(**overrides):
    base = {
        "trade_date": date(2026, 8, 4),
        "run_calculated_at": datetime(2026, 8, 4, 15, 0, 0),
        "algorithm_versions": build_default_algorithm_versions(),
        "config": {"bollinger": {"window": 20}},
    }
    base.update(overrides)
    return CoreRunContext(**base)


def test_parameter_hash_is_stable():
    """相同配置/版本 → 相同 parameter_hash。"""
    a = _make_context()
    b = _make_context()
    assert a.parameter_hash == b.parameter_hash


def test_parameter_hash_differs_when_config_changes():
    """配置变化 → parameter_hash 变化。"""
    a = _make_context()
    b = _make_context(config={"bollinger": {"window": 30}})
    assert a.parameter_hash != b.parameter_hash


def test_parameter_hash_differs_when_version_changes():
    """算法版本变化 → parameter_hash 变化。"""
    a = _make_context()
    versions = build_default_algorithm_versions()
    versions["dsa"] = "dsa-v2"
    b = _make_context(algorithm_versions=versions)
    assert a.parameter_hash != b.parameter_hash


def test_parameter_hash_order_independent_config():
    """config 键顺序无关。"""
    a = _make_context(config={"a": 1, "b": {"c": 2}})
    b = _make_context(config={"b": {"c": 2}, "a": 1})
    assert a.parameter_hash == b.parameter_hash


def test_artifact_availability_gate():
    """availability 全部 ready → is_available；任一缺失 → False。"""
    full = CoreComputationArtifact(
        instrument_id="600000",
        trade_date=date(2026, 8, 4),
        availability={"structure": "ready", "smc": "ready", "momentum": "ready"},
    )
    assert full.is_available

    missing = CoreComputationArtifact(
        instrument_id="600000",
        trade_date=date(2026, 8, 4),
        availability={"structure": "ready", "smc": "unavailable", "momentum": "ready"},
    )
    assert not missing.is_available


def test_artifact_to_dict_roundtrip():
    """to_dict 包含关键字段。"""
    art = CoreComputationArtifact(
        instrument_id="600000",
        trade_date=date(2026, 8, 4),
        payload={"firstPyramidCore": {}},
        availability={"structure": "ready", "smc": "ready", "momentum": "ready"},
    )
    d = art.to_dict()
    assert d["instrument_id"] == "600000"
    assert d["trade_date"] == "2026-08-04"
    assert "payload" in d and "availability" in d
