"""套餐服务层 - 查询 plans 表套餐定义。

提供：
- get_plan: 按 plan_code 查询 Plan 对象，未知 plan_code 抛 ValueError
- get_plan_by_code: get_plan 的别名（语义化命名）
- list_all_plans: 列出所有 active 套餐
- get_monitor_limit: 按 plan_code 查询监控数量上限（替代旧 plan_contract.get_monitor_limit）

业务规则：
- plans 表是套餐定义的唯一真源（observe_20/research_50）
- 未知 plan_code 抛 ValueError（与旧 plan_contract.get_monitor_limit 抛 KeyError 不同，
  改为 ValueError 与 subscription_service.generate_invite_codes 的异常类型一致）
- 所有函数为 async，需传入 db: AsyncSession（旧 plan_contract 为同步字典查询）
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan


async def get_plan(db: AsyncSession, plan_code: str) -> Plan:
    """按 plan_code 查询 Plan 对象。

    Args:
        db: 异步数据库会话
        plan_code: 套餐代码（observe_20/research_50）

    Returns:
        Plan ORM 对象

    Raises:
        ValueError: plan_code 不在 plans 表中
    """
    stmt = select(Plan).where(Plan.plan_code == plan_code)
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()
    if plan is None:
        raise ValueError(f"未知套餐代码: {plan_code}")
    return plan


# [PlanService] - 描述: get_plan 的语义化别名，便于调用方按 plan_code 命名引用
get_plan_by_code = get_plan


async def list_all_plans(db: AsyncSession) -> list[Plan]:
    """列出所有 active 状态的套餐（按 plan_code 排序）。

    Args:
        db: 异步数据库会话

    Returns:
        Plan 对象列表
    """
    stmt = (
        select(Plan)
        .where(Plan.status == "active")
        .order_by(Plan.plan_code.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_monitor_limit(db: AsyncSession, plan_code: str) -> int:
    """按 plan_code 查询监控数量上限。

    替代旧 app.constants.plan_contract.get_monitor_limit（同步字典查询）。
    本函数为 async，需传入 db。

    Args:
        db: 异步数据库会话
        plan_code: 套餐代码

    Returns:
        监控数量上限

    Raises:
        ValueError: plan_code 不在 plans 表中
    """
    plan = await get_plan(db, plan_code)
    return int(plan.monitor_limit)


if __name__ == "__main__":
    # [PlanService] - 描述: 自测入口，验证函数签名与别名（不连接数据库）
    assert callable(get_plan)
    assert callable(get_plan_by_code)
    assert callable(list_all_plans)
    assert callable(get_monitor_limit)
    # get_plan_by_code 必须是 get_plan 的别名（同一函数对象）
    assert get_plan_by_code is get_plan
    print(f"get_plan={get_plan}")
    print(f"get_plan_by_code is get_plan: {get_plan_by_code is get_plan}")
    print("OK: plan_service 函数签名验证通过")
