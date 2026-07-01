"""套餐代码字符串常量 - 仅 plan_code 标识，套餐定义在 plans 表。

用法:
    from app.constants.plan_codes import DEFAULT_PLAN_CODE
    python -m app.constants.plan_codes  # 打印常量供人工校验

说明:
    - 套餐定义（monitor_limit/notification_channel_limit/features 等）存储在 plans 表
    - 本文件仅保留 plan_code 字符串常量，供函数默认参数与代码引用使用
    - DEFAULT_PLAN_CODE: 旧数据迁移默认套餐（observe_20）
    - 管理员不绑定任何套餐，故不保留 ADMIN_PLAN_CODE（AGENTS.md 规则 8）
    - 业务代码查询套餐字段请用 app.services.plan_service.get_plan
"""

from __future__ import annotations

# [PlanCodes] - 描述: 旧数据迁移默认套餐（observe_20）
DEFAULT_PLAN_CODE: str = "observe_20"


if __name__ == "__main__":
    # [PlanCodes] - 描述: 自测入口，验证常量值与类型
    assert DEFAULT_PLAN_CODE == "observe_20"
    assert isinstance(DEFAULT_PLAN_CODE, str)
    print(f"DEFAULT_PLAN_CODE = {DEFAULT_PLAN_CODE!r}")
    print("OK")
