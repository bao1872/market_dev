"""plan_contract 迁移到 plans 表后的契约一致性测试（Phase 2 Task 2.1）。

原 test_plan_contract.py 测试 app.constants.plan_contract.PLAN_CONTRACTS 字典；
迁移后套餐定义存储在 plans 表，本测试验证新结构保持相同契约：
- observe_20: monitor_limit=20, display_name=观察版
- research_50: monitor_limit=50, display_name=研究版
- 未知 plan_code 查询抛 ValueError（旧为 KeyError）
- plan_codes.py 提供 DEFAULT_PLAN_CODE/ADMIN_PLAN_CODE 纯字符串常量
- plan_contract.py 模块已删除（import 应失败）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库，已 alembic upgrade head）
- 调用 plan_service.get_plan/get_monitor_limit 验证套餐字段
- 验证 plan_codes.py 常量值
"""

from __future__ import annotations

import importlib

import pytest

from app.constants.plan_codes import ADMIN_PLAN_CODE, DEFAULT_PLAN_CODE
from app.services.plan_service import get_monitor_limit, get_plan

# ============================================================
# plan_contract.py 模块已删除验证
# ============================================================


def test_plan_contract_module_removed():
    """plan_contract.py 模块应已删除，import 必须失败。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.constants.plan_contract")


# ============================================================
# plan_codes.py 纯字符串常量验证
# ============================================================


def test_default_plan_code_is_observe_20():
    """DEFAULT_PLAN_CODE 必须为 observe_20（旧数据迁移默认值）。"""
    assert DEFAULT_PLAN_CODE == "observe_20"


def test_admin_plan_code_is_research_50():
    """ADMIN_PLAN_CODE 必须为 research_50（管理员默认套餐）。"""
    assert ADMIN_PLAN_CODE == "research_50"


# ============================================================
# plans 表套餐契约验证（替代旧 PLAN_CONTRACTS 字典）
# ============================================================


@pytest.mark.asyncio
async def test_observe_20_monitor_limit_is_20(db_session):
    """observe_20 套餐 monitor_limit 必须为 20。"""
    limit = await get_monitor_limit(db_session, "observe_20")
    assert limit == 20


@pytest.mark.asyncio
async def test_research_50_monitor_limit_is_50(db_session):
    """research_50 套餐 monitor_limit 必须为 50。"""
    limit = await get_monitor_limit(db_session, "research_50")
    assert limit == 50


@pytest.mark.asyncio
async def test_observe_20_display_name_not_empty(db_session):
    """observe_20 套餐 display_name 必须为非空字符串（替代旧 get_plan_name）。"""
    plan = await get_plan(db_session, "observe_20")
    assert isinstance(plan.display_name, str)
    assert len(plan.display_name) > 0
    assert plan.display_name == "观察版"


@pytest.mark.asyncio
async def test_research_50_display_name_not_empty(db_session):
    """research_50 套餐 display_name 必须为非空字符串。"""
    plan = await get_plan(db_session, "research_50")
    assert isinstance(plan.display_name, str)
    assert len(plan.display_name) > 0
    assert plan.display_name == "研究版"


@pytest.mark.asyncio
async def test_get_monitor_limit_unknown_raises_value_error(db_session):
    """get_monitor_limit 未知 plan_code 抛 ValueError（旧为 KeyError）。"""
    with pytest.raises(ValueError, match="未知套餐代码"):
        await get_monitor_limit(db_session, "unknown_plan")


@pytest.mark.asyncio
async def test_get_plan_unknown_raises_value_error(db_session):
    """get_plan 未知 plan_code 抛 ValueError。"""
    with pytest.raises(ValueError, match="未知套餐代码"):
        await get_plan(db_session, "unknown_plan")


@pytest.mark.asyncio
async def test_observe_20_monitor_limit_less_than_research_50(db_session):
    """observe_20 的 monitor_limit 必须小于 research_50（套餐层级关系）。"""
    observe_limit = await get_monitor_limit(db_session, "observe_20")
    research_limit = await get_monitor_limit(db_session, "research_50")
    assert observe_limit < research_limit


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
