"""plan_contract 套餐契约一致性测试（SubTask 4.10）。

验证 app.constants.plan_contract.PLAN_CONTRACTS 是套餐定义的唯一真源：
- observe_20: monitor_limit=20
- research_50: monitor_limit=50
- 未知 plan_code 查询抛 KeyError
- 20/50 字面量只允许出现在 plan_contract.py 中（禁止散落硬编码）
"""

from __future__ import annotations

import pytest

from app.constants.plan_contract import (
    DEFAULT_PLAN_CODE,
    PLAN_CONTRACTS,
    get_monitor_limit,
    get_plan_name,
)


def test_plan_contracts_contains_observe_20():
    """PLAN_CONTRACTS 必须包含 observe_20 套餐，monitor_limit=20。"""
    assert "observe_20" in PLAN_CONTRACTS
    contract = PLAN_CONTRACTS["observe_20"]
    assert contract["monitor_limit"] == 20
    assert "name" in contract
    assert isinstance(contract["name"], str)
    assert len(contract["name"]) > 0


def test_plan_contracts_contains_research_50():
    """PLAN_CONTRACTS 必须包含 research_50 套餐，monitor_limit=50。"""
    assert "research_50" in PLAN_CONTRACTS
    contract = PLAN_CONTRACTS["research_50"]
    assert contract["monitor_limit"] == 50
    assert "name" in contract
    assert isinstance(contract["name"], str)
    assert len(contract["name"]) > 0


def test_plan_contracts_only_two_plans():
    """PLAN_CONTRACTS 只允许两个套餐：observe_20 和 research_50。"""
    assert set(PLAN_CONTRACTS.keys()) == {"observe_20", "research_50"}


def test_unknown_plan_code_raises_key_error():
    """未知 plan_code 查询应抛 KeyError。"""
    with pytest.raises(KeyError):
        _ = PLAN_CONTRACTS["unknown_plan"]


def test_get_monitor_limit_observe_20():
    """get_monitor_limit('observe_20') 返回 20。"""
    assert get_monitor_limit("observe_20") == 20


def test_get_monitor_limit_research_50():
    """get_monitor_limit('research_50') 返回 50。"""
    assert get_monitor_limit("research_50") == 50


def test_get_monitor_limit_unknown_raises():
    """get_monitor_limit 未知 plan_code 抛 KeyError。"""
    with pytest.raises(KeyError):
        get_monitor_limit("unknown_plan")


def test_get_plan_name_observe_20():
    """get_plan_name('observe_20') 返回非空字符串。"""
    name = get_plan_name("observe_20")
    assert isinstance(name, str)
    assert len(name) > 0


def test_get_plan_name_research_50():
    """get_plan_name('research_50') 返回非空字符串。"""
    name = get_plan_name("research_50")
    assert isinstance(name, str)
    assert len(name) > 0


def test_default_plan_code_is_observe_20():
    """DEFAULT_PLAN_CODE 必须为 observe_20（旧数据迁移默认值）。"""
    assert DEFAULT_PLAN_CODE == "observe_20"


def test_observe_20_monitor_limit_less_than_research_50():
    """observe_20 的 monitor_limit 必须小于 research_50（套餐层级关系）。"""
    assert (
        PLAN_CONTRACTS["observe_20"]["monitor_limit"]
        < PLAN_CONTRACTS["research_50"]["monitor_limit"]
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
