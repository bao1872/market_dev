"""套餐代码字符串常量 - 仅 plan_code 标识，套餐定义在 plans 表。

用法:
    from app.constants.plan_codes import DEFAULT_PLAN_CODE, ADMIN_PLAN_CODE
    python -m app.constants.plan_codes  # 打印常量供人工校验

说明:
    - 套餐定义（monitor_limit/notification_channel_limit/features 等）存储在 plans 表
    - 本文件仅保留 plan_code 字符串常量，供函数默认参数与代码引用使用
    - DEFAULT_PLAN_CODE: 旧数据迁移默认套餐（observe_20）
    - ADMIN_PLAN_CODE: 管理员默认套餐（research_50，绕过 monitor_limit 校验但记录套餐）
    - 业务代码查询套餐字段请用 app.services.plan_service.get_plan
"""

from __future__ import annotations

# [PlanCodes] - 描述: 旧数据迁移默认套餐（observe_20）
DEFAULT_PLAN_CODE: str = "observe_20"

# [PlanCodes] - 描述: 管理员默认套餐（admin 绕过 monitor_limit 校验，但记录 plan_code）
ADMIN_PLAN_CODE: str = "research_50"


if __name__ == "__main__":
    # [PlanCodes] - 描述: 自测入口，验证常量值与类型
    assert DEFAULT_PLAN_CODE == "observe_20"
    assert ADMIN_PLAN_CODE == "research_50"
    assert isinstance(DEFAULT_PLAN_CODE, str)
    assert isinstance(ADMIN_PLAN_CODE, str)
    print(f"DEFAULT_PLAN_CODE = {DEFAULT_PLAN_CODE!r}")
    print(f"ADMIN_PLAN_CODE = {ADMIN_PLAN_CODE!r}")
    print("OK")
