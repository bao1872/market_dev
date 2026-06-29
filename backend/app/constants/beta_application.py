"""内测申请常量枚举 - reason_code 与 status 的唯一权威定义。

用法:
    from app.constants.beta_application import (
        REASON_CODES,
        BETA_APPLICATION_STATUSES,
        BETA_APPLICATION_STATUSES_DEFAULT,
    )

说明:
    - REASON_CODES 是用户使用理由枚举的唯一真源
    - BETA_APPLICATION_STATUSES 是申请状态枚举的唯一真源
    - 字面量 'busy'/'too_many'/'forget'/'quant'/'other' 只允许出现在本文件
    - 字面量 'new'/'contacted'/'approved'/'rejected'/'converted' 只允许出现在本文件
    - 其他代码必须导入引用，禁止散落硬编码
"""

from __future__ import annotations

# [beta_application] - 描述: 使用理由枚举（用户选择）
REASON_CODES: list[str] = ["busy", "too_many", "forget", "quant", "other"]

# [beta_application] - 描述: 申请状态枚举（管理员流转）
# new: 新申请（默认）
# contacted: 已联系
# approved: 已通过
# rejected: 已拒绝
# converted: 已转化（注册成功）
BETA_APPLICATION_STATUSES: list[str] = [
    "new",
    "contacted",
    "approved",
    "rejected",
    "converted",
]

# 默认状态（创建时使用）
BETA_APPLICATION_STATUSES_DEFAULT: str = "new"

# 飞书投递状态枚举
FEISHU_DELIVERY_STATUSES: list[str] = ["pending", "success", "failed"]


def is_valid_reason_code(code: str) -> bool:
    """校验 reason_code 是否合法。

    Args:
        code: 待校验的理由代码

    Returns:
        True 如 code 在 REASON_CODES 中，否则 False
    """
    return code in REASON_CODES


def is_valid_status(status: str) -> bool:
    """校验 status 是否合法。

    Args:
        status: 待校验的状态

    Returns:
        True 如 status 在 BETA_APPLICATION_STATUSES 中，否则 False
    """
    return status in BETA_APPLICATION_STATUSES


if __name__ == "__main__":
    print("=" * 60)
    print("内测申请常量 (beta_application.py)")
    print("=" * 60)
    print(f"  REASON_CODES = {REASON_CODES}")
    print(f"  BETA_APPLICATION_STATUSES = {BETA_APPLICATION_STATUSES}")
    print(f"  BETA_APPLICATION_STATUSES_DEFAULT = {BETA_APPLICATION_STATUSES_DEFAULT!r}")
    print(f"  FEISHU_DELIVERY_STATUSES = {FEISHU_DELIVERY_STATUSES}")
    print("=" * 60)
    assert set(REASON_CODES) == {"busy", "too_many", "forget", "quant", "other"}
    assert set(BETA_APPLICATION_STATUSES) == {
        "new", "contacted", "approved", "rejected", "converted",
    }
    assert BETA_APPLICATION_STATUSES_DEFAULT == "new"
    assert is_valid_reason_code("busy") is True
    assert is_valid_reason_code("invalid") is False
    assert is_valid_status("new") is True
    assert is_valid_status("invalid") is False
    print("OK")
