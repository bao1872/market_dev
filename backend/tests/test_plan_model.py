"""plans 表模型与 plan_service 服务层测试（Phase 2 Task 2.1）。

验证套餐定义从 plan_contract.py 字典迁移到 plans 表后的正确性：
- plans 表存在 observe_20 / research_50 两条记录，字段值与 permission-matrix.md 一致
- plan_service.get_plan 查询返回完整 Plan 对象，未知 plan_code 抛 ValueError
- plan_codes.py 仅保留纯字符串常量 DEFAULT_PLAN_CODE / ADMIN_PLAN_CODE

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库，已 alembic upgrade head）
- 直接查询 plans 表，验证字段值
- 调用 plan_service 函数，验证返回值与异常
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.constants.plan_codes import ADMIN_PLAN_CODE, DEFAULT_PLAN_CODE
from app.models.plan import Plan
from app.services.plan_service import get_monitor_limit, get_plan

# ============================================================
# plans 表数据完整性测试
# ============================================================


@pytest.mark.asyncio
async def test_plans_table_has_observe_20(db_session):
    """plans 表必须包含 observe_20 套餐，字段值与 permission-matrix.md 一致。"""
    result = await db_session.execute(
        select(Plan).where(Plan.plan_code == "observe_20")
    )
    plan = result.scalar_one_or_none()
    assert plan is not None, "plans 表缺少 observe_20 记录"
    assert plan.plan_code == "observe_20"
    assert plan.display_name == "观察版"
    assert plan.monitor_limit == 20
    assert plan.notification_channel_limit == 1
    assert plan.message_retention_days == 30
    # features 含 6 项
    assert isinstance(plan.features, list)
    assert len(plan.features) == 6
    expected_features = {
        "trend_selection",
        "stock_detail",
        "node_monitor",
        "in_app_message",
        "feishu_notification",
        "stock_memo",
    }
    assert set(plan.features) == expected_features


@pytest.mark.asyncio
async def test_plans_table_has_research_50(db_session):
    """plans 表必须包含 research_50 套餐，字段值与 permission-matrix.md 一致。"""
    result = await db_session.execute(
        select(Plan).where(Plan.plan_code == "research_50")
    )
    plan = result.scalar_one_or_none()
    assert plan is not None, "plans 表缺少 research_50 记录"
    assert plan.plan_code == "research_50"
    assert plan.display_name == "研究版"
    assert plan.monitor_limit == 50
    assert plan.notification_channel_limit == 3
    assert plan.message_retention_days == 180
    # features 含 7 项（含 advanced_export）
    assert isinstance(plan.features, list)
    assert len(plan.features) == 7
    assert "advanced_export" in plan.features


@pytest.mark.asyncio
async def test_plans_table_only_two_records(db_session):
    """plans 表只允许 observe_20 和 research_50 两条记录。"""
    result = await db_session.execute(select(Plan))
    plans = result.scalars().all()
    codes = {p.plan_code for p in plans}
    assert codes == {"observe_20", "research_50"}, (
        f"plans 表应只有 observe_20/research_50，实际: {codes}"
    )
    assert len(plans) == 2


@pytest.mark.asyncio
async def test_plans_table_has_unique_constraint_on_plan_code(db_session):
    """plan_code 列必须有唯一约束（防止重复插入）。"""
    result = await db_session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename='plans' AND indexdef LIKE '%UNIQUE%'"
        )
    )
    rows = result.all()
    # 至少有一个唯一索引包含 plan_code
    assert len(rows) > 0, "plans 表缺少唯一索引"
    assert any("plan_code" in row[1] for row in rows), (
        "plans 表缺少 plan_code 唯一索引"
    )


# ============================================================
# plan_service 服务层测试
# ============================================================


@pytest.mark.asyncio
async def test_plan_service_get_plan_returns_correct_data(db_session):
    """plan_service.get_plan(db, 'observe_20') 返回完整 Plan 对象。"""
    plan = await get_plan(db_session, "observe_20")
    assert isinstance(plan, Plan)
    assert plan.plan_code == "observe_20"
    assert plan.monitor_limit == 20
    assert plan.display_name == "观察版"


@pytest.mark.asyncio
async def test_plan_service_get_plan_research_50(db_session):
    """plan_service.get_plan(db, 'research_50') 返回 research_50 Plan 对象。"""
    plan = await get_plan(db_session, "research_50")
    assert isinstance(plan, Plan)
    assert plan.plan_code == "research_50"
    assert plan.monitor_limit == 50


@pytest.mark.asyncio
async def test_plan_service_get_plan_raises_on_unknown(db_session):
    """plan_service.get_plan(db, 'unknown') 抛 ValueError。"""
    with pytest.raises(ValueError, match="未知套餐代码"):
        await get_plan(db_session, "unknown_plan")


@pytest.mark.asyncio
async def test_plan_service_get_monitor_limit_observe_20(db_session):
    """plan_service.get_monitor_limit(db, 'observe_20') 返回 20。"""
    limit = await get_monitor_limit(db_session, "observe_20")
    assert limit == 20


@pytest.mark.asyncio
async def test_plan_service_get_monitor_limit_research_50(db_session):
    """plan_service.get_monitor_limit(db, 'research_50') 返回 50。"""
    limit = await get_monitor_limit(db_session, "research_50")
    assert limit == 50


@pytest.mark.asyncio
async def test_plan_service_get_monitor_limit_unknown_raises(db_session):
    """plan_service.get_monitor_limit 未知 plan_code 抛 ValueError。"""
    with pytest.raises(ValueError, match="未知套餐代码"):
        await get_monitor_limit(db_session, "unknown_plan")


# ============================================================
# plan_codes.py 纯字符串常量测试
# ============================================================


def test_plan_codes_constants_exist():
    """plan_codes.py 必须提供 DEFAULT_PLAN_CODE 和 ADMIN_PLAN_CODE 纯字符串常量。"""
    assert DEFAULT_PLAN_CODE == "observe_20"
    assert ADMIN_PLAN_CODE == "research_50"
    # 必须是字符串类型（不是函数/对象）
    assert isinstance(DEFAULT_PLAN_CODE, str)
    assert isinstance(ADMIN_PLAN_CODE, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
