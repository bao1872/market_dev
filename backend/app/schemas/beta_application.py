"""内测申请 Pydantic schemas - 公开端点请求/响应 + 管理后台响应（服务端校验）。

提供：
- BetaApplicationCreate: 公开端点请求 schema（服务端校验至少一个联系方式、手机格式、
  reason_code 枚举、其他必填说明、隐私同意）
- BetaApplicationResponse: 公开端点响应 schema（不返回完整联系方式，保护隐私）
- BetaApplicationListItem: 管理后台列表项 schema（含完整字段，仅 admin 可见）
- BetaApplicationListResponse: 管理后台列表响应（{items, total, limit, offset}）
- BetaApplicationAdminResponse: 管理后台详情响应（含飞书投递信息）
- BetaApplicationStatsResponse: 管理后台统计响应
- BetaApplicationPatchRequest: 管理后台状态更新请求（status + admin_note）
- RetryFeishuResponse: 重发飞书响应（outbox_id + message）

服务端校验规则（spec 第三节）：
- wechat/phone 至少填一个（model_validator）
- phone 格式：中国大陆手机号 1[3-9]\\d{9}（field_validator）
- reason_code 必须在 REASON_CODES 枚举内（Literal）
- reason_code='other' 时 reason_other 必填（model_validator）
- watch_stock_count 必须 >= 1（Field ge=1）
- privacy_agreed 必须 True（field_validator）
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants.beta_application import REASON_CODES

# 中国大陆手机号正则：1[3-9] 开头 + 9 位数字 = 11 位
_PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")


class BetaApplicationCreate(BaseModel):
    """内测申请创建请求 - 公开端点 POST /public/beta-applications。

    服务端校验（spec 第三节）：
    - wechat/phone 至少填一个
    - phone 格式：中国大陆手机号
    - reason_code 枚举校验
    - reason_code='other' 时 reason_other 必填
    - watch_stock_count 正整数
    - privacy_agreed 必须 True
    """

    wechat: str | None = Field(
        default=None, max_length=64, description="微信号（与 phone 至少填一个）"
    )
    phone: str | None = Field(
        default=None, max_length=32, description="手机号（与 wechat 至少填一个）"
    )
    watch_stock_count: int = Field(
        ..., ge=1, le=10000, description="盯盘股票数量（正整数，1-10000）"
    )
    reason_code: str = Field(
        ..., description=f"使用理由代码（{', '.join(REASON_CODES)}）"
    )
    reason_other: str | None = Field(
        default=None, max_length=2000, description="补充说明（reason_code='other' 时必填）"
    )
    privacy_agreed: bool = Field(
        ..., description="隐私同意（必须 True）"
    )
    source: str | None = Field(
        default=None, max_length=64, description="提交来源（由前端传入，用于追踪）"
    )

    @field_validator("phone")
    @classmethod
    def validate_phone_format(cls, v: str | None) -> str | None:
        """校验手机号格式（中国大陆 1[3-9]\\d{9}）。"""
        if v is None or v == "":
            return None
        if not _PHONE_PATTERN.match(v):
            raise ValueError(f"手机号格式非法（需为 11 位中国大陆手机号）: {v!r}")
        return v

    @field_validator("wechat")
    @classmethod
    def normalize_wechat(cls, v: str | None) -> str | None:
        """归一化微信号（去除首尾空格，空字符串转 None）。"""
        if v is None:
            return None
        v = v.strip()
        return v if v else None

    @field_validator("reason_other")
    @classmethod
    def normalize_reason_other(cls, v: str | None) -> str | None:
        """归一化补充说明（去除首尾空格，空字符串转 None）。"""
        if v is None:
            return None
        v = v.strip()
        return v if v else None

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, v: str) -> str:
        """校验 reason_code 在枚举内。"""
        if v not in REASON_CODES:
            raise ValueError(
                f"reason_code 必须为 {REASON_CODES} 之一，实际: {v!r}"
            )
        return v

    @field_validator("privacy_agreed")
    @classmethod
    def validate_privacy_agreed(cls, v: bool) -> bool:
        """隐私同意必须为 True。"""
        if not v:
            raise ValueError("必须勾选隐私同意")
        return v

    @model_validator(mode="after")
    def validate_at_least_one_contact(self) -> BetaApplicationCreate:
        """校验 wechat/phone 至少填一个。"""
        if not self.wechat and not self.phone:
            raise ValueError("请至少填写一种联系方式（微信号或手机号）")
        return self

    @model_validator(mode="after")
    def validate_other_requires_reason(self) -> BetaApplicationCreate:
        """reason_code='other' 时 reason_other 必填。"""
        if self.reason_code == "other" and not self.reason_other:
            raise ValueError("选择'其他'理由时必须填写补充说明")
        return self


class BetaApplicationResponse(BaseModel):
    """内测申请响应 - 不返回完整联系方式（隐私保护）。

    仅返回申请编号、提交时间、状态，供用户确认提交成功。
    完整联系方式仅管理员后台可见。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="申请编号")
    status: str = Field(..., description="申请状态")
    submitted_at: datetime = Field(..., description="提交时间")


# ============================================================
# 管理后台 schemas（Task 4）- 仅 admin 角色可见，含完整联系方式
# ============================================================


class BetaApplicationListItem(BaseModel):
    """管理后台列表项 - 含完整联系方式与状态（仅 admin 可见）。

    用于 GET /admin/beta-applications 列表响应。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="申请编号")
    wechat: str | None = Field(None, description="微信号")
    phone: str | None = Field(None, description="手机号")
    watch_stock_count: int = Field(..., description="盯盘股票数量")
    reason_code: str = Field(..., description="使用理由代码")
    reason_other: str | None = Field(None, description="补充说明")
    status: str = Field(..., description="申请状态")
    source: str | None = Field(None, description="提交来源")
    admin_note: str | None = Field(None, description="管理员备注")
    handled_by: UUID | None = Field(None, description="处理人 user_id")
    handled_at: datetime | None = Field(None, description="处理时间")
    submitted_at: datetime = Field(..., description="提交时间")
    updated_at: datetime = Field(..., description="更新时间")
    feishu_delivery_status: str | None = Field(None, description="飞书投递状态")


class BetaApplicationListResponse(BaseModel):
    """管理后台列表响应 - {items, total, limit, offset}。"""

    items: list[BetaApplicationListItem] = Field(default_factory=list, description="列表项")
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="分页大小")
    offset: int = Field(..., description="分页偏移")


class BetaApplicationAdminResponse(BaseModel):
    """管理后台详情响应 - 含完整字段与飞书投递信息（仅 admin 可见）。

    用于 GET /admin/beta-applications/{id} 详情 + PATCH 更新后返回。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="申请编号")
    wechat: str | None = Field(None, description="微信号")
    phone: str | None = Field(None, description="手机号")
    watch_stock_count: int = Field(..., description="盯盘股票数量")
    reason_code: str = Field(..., description="使用理由代码")
    reason_other: str | None = Field(None, description="补充说明")
    status: str = Field(..., description="申请状态")
    source: str | None = Field(None, description="提交来源")
    admin_note: str | None = Field(None, description="管理员备注")
    handled_by: UUID | None = Field(None, description="处理人 user_id")
    handled_at: datetime | None = Field(None, description="处理时间")
    submitted_at: datetime = Field(..., description="提交时间")
    updated_at: datetime = Field(..., description="更新时间")
    ip_hash: str = Field(..., description="客户端 IP 哈希")
    feishu_delivery_status: str | None = Field(None, description="飞书投递状态")
    feishu_delivered_at: datetime | None = Field(None, description="飞书投递成功时间")
    feishu_last_error: str | None = Field(None, description="飞书投递最近错误")


class BetaApplicationStatsResponse(BaseModel):
    """管理后台统计响应 - 统计卡数据。

    字段对应 beta_application_service.get_admin_stats 返回的字典。
    """

    model_config = ConfigDict(from_attributes=True)

    total: int = Field(..., description="累计申请数")
    today: int = Field(..., description="今日新增")
    last_7_days: int = Field(..., description="近 7 天新增")
    last_30_days: int = Field(..., description="近 30 天新增")
    by_status: dict[str, int] = Field(default_factory=dict, description="各状态计数")
    avg_watch_stock_count: float = Field(..., description="平均盯盘数")
    by_reason: dict[str, int] = Field(default_factory=dict, description="理由占比")
    by_watch_range: dict[str, int] = Field(default_factory=dict, description="股票数量区间分布")


class BetaApplicationPatchRequest(BaseModel):
    """管理后台状态更新请求 - PATCH /admin/beta-applications/{id}。

    status 必填（new/contacted/approved/rejected/converted），
    admin_note 可选（管理员备注）。
    """

    status: str = Field(..., description="新状态（new/contacted/approved/rejected/converted）")
    admin_note: str | None = Field(default=None, max_length=2000, description="管理员备注")


class RetryFeishuResponse(BaseModel):
    """重发飞书响应 - POST /admin/beta-applications/{id}/retry-feishu。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="申请编号")
    outbox_id: UUID = Field(..., description="新创建的 Outbox 记录 ID")
    message: str = Field(default="飞书重发已入队", description="操作结果消息")


if __name__ == "__main__":
    # 自测入口：验证 schema 校验逻辑
    # 合法 payload
    obj = BetaApplicationCreate(
        wechat="test_wechat",
        watch_stock_count=10,
        reason_code="busy",
        privacy_agreed=True,
    )
    assert obj.wechat == "test_wechat"
    print(f"合法 payload: {obj}")

    # 合法 payload（仅手机号）
    obj2 = BetaApplicationCreate(
        phone="13800138000",
        watch_stock_count=5,
        reason_code="quant",
        privacy_agreed=True,
    )
    assert obj2.phone == "13800138000"
    print(f"合法 payload（仅手机号）: {obj2}")

    # 非法：无联系方式
    from pydantic import ValidationError

    try:
        BetaApplicationCreate(
            watch_stock_count=10, reason_code="busy", privacy_agreed=True
        )
        raise AssertionError("应抛出无联系方式异常")
    except ValidationError as e:
        print(f"无联系方式已拒绝: {str(e)[:80]}")

    # 非法：other 无说明
    try:
        BetaApplicationCreate(
            wechat="test",
            watch_stock_count=10,
            reason_code="other",
            privacy_agreed=True,
        )
        raise AssertionError("应抛出 other 无说明异常")
    except ValidationError as e:
        print(f"other 无说明已拒绝: {str(e)[:80]}")

    # 非法：手机格式
    try:
        BetaApplicationCreate(
            phone="12345",
            watch_stock_count=10,
            reason_code="busy",
            privacy_agreed=True,
        )
        raise AssertionError("应抛出手机格式异常")
    except ValidationError as e:
        print(f"手机格式非法已拒绝: {str(e)[:80]}")

    # 验证管理后台 schema 字段（Task 4）
    patch_req = BetaApplicationPatchRequest(status="contacted", admin_note="已联系")
    assert patch_req.status == "contacted"
    assert patch_req.admin_note == "已联系"
    print(f"PATCH 请求: {patch_req}")

    stats_resp = BetaApplicationStatsResponse(
        total=10, today=2, last_7_days=5, last_30_days=8,
        by_status={"new": 5, "contacted": 3, "approved": 2},
        avg_watch_stock_count=15.5,
        by_reason={"busy": 4, "quant": 6},
        by_watch_range={"1-10": 5, "11-20": 3, "21-50": 2},
    )
    assert stats_resp.total == 10
    assert stats_resp.by_status["new"] == 5
    print(f"统计响应: total={stats_resp.total}, by_status={stats_resp.by_status}")

    retry_resp = RetryFeishuResponse(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        outbox_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    assert retry_resp.message == "飞书重发已入队"
    print(f"重发飞书响应: {retry_resp}")

    # 验证列表响应 schema
    list_resp = BetaApplicationListResponse(items=[], total=0, limit=20, offset=0)
    assert list_resp.total == 0
    assert list_resp.items == []
    print(f"空列表响应: {list_resp}")

    print("OK")
