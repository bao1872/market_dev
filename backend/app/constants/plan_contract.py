"""套餐契约 - 门户收费模式套餐定义的唯一真源。

用法:
    from app.constants.plan_contract import PLAN_CONTRACTS, get_monitor_limit
    python -m app.constants.plan_contract  # 打印所有套餐供人工校验

说明:
    - PLAN_CONTRACTS 是 observe_20 / research_50 套餐的唯一权威定义
    - monitor_limit 字面量 20/50 只允许出现在本文件，其它代码必须导入引用
    - 旧邀请码和旧会员迁移时默认映射为 DEFAULT_PLAN_CODE='observe_20'
    - 管理员角色默认套餐为 research_50（绕过 monitor_limit 校验但记录套餐）
"""

from __future__ import annotations

# [plan_contract] - 描述: 套餐契约唯一真源，monitor_limit 字面量 20/50 只允许在此定义
PLAN_CONTRACTS: dict[str, dict[str, object]] = {
    "observe_20": {
        "monitor_limit": 20,
        "name": "观察版",
    },
    "research_50": {
        "monitor_limit": 50,
        "name": "研究版",
    },
}

# 旧数据迁移默认套餐（旧邀请码和旧会员回填用）
DEFAULT_PLAN_CODE: str = "observe_20"

# 管理员默认套餐（admin 绕过 monitor_limit 校验，但记录 plan_code）
ADMIN_PLAN_CODE: str = "research_50"


def get_monitor_limit(plan_code: str) -> int:
    """按 plan_code 查询监控上限。

    Args:
        plan_code: 套餐代码（observe_20 / research_50）

    Returns:
        监控数量上限

    Raises:
        KeyError: plan_code 不在 PLAN_CONTRACTS 中
    """
    return int(PLAN_CONTRACTS[plan_code]["monitor_limit"])


def get_plan_name(plan_code: str) -> str:
    """按 plan_code 查询套餐展示名。

    Args:
        plan_code: 套餐代码

    Returns:
        套餐展示名（如"观察版"/"研究版"）

    Raises:
        KeyError: plan_code 不在 PLAN_CONTRACTS 中
    """
    return str(PLAN_CONTRACTS[plan_code]["name"])


def is_valid_plan_code(plan_code: str) -> bool:
    """校验 plan_code 是否为已定义套餐。

    Args:
        plan_code: 待校验的套餐代码

    Returns:
        True 如 plan_code 在 PLAN_CONTRACTS 中，否则 False
    """
    return plan_code in PLAN_CONTRACTS


if __name__ == "__main__":
    print("=" * 60)
    print("套餐契约 (plan_contract.py)")
    print("=" * 60)
    for code, contract in PLAN_CONTRACTS.items():
        print(f"  {code}: monitor_limit={contract['monitor_limit']}, name={contract['name']}")
    print(f"  DEFAULT_PLAN_CODE = {DEFAULT_PLAN_CODE!r}")
    print(f"  ADMIN_PLAN_CODE = {ADMIN_PLAN_CODE!r}")
    print("=" * 60)
    assert get_monitor_limit("observe_20") == 20
    assert get_monitor_limit("research_50") == 50
    assert get_plan_name("observe_20") == "观察版"
    assert get_plan_name("research_50") == "研究版"
    assert is_valid_plan_code("observe_20") is True
    assert is_valid_plan_code("unknown") is False
    print("OK")
