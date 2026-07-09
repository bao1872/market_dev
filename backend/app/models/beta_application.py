"""内测申请 ORM 模型 - 用户站内提交的内测申请。

对应迁移 045_beta_applications：
- beta_applications: 内测申请表（id/wechat/phone/watch_stock_count/reason_code/
  reason_other/status/source/admin_note/handled_by/handled_at/submitted_at/
  updated_at/ip_hash/feishu_delivery_status/feishu_delivered_at/feishu_last_error）

设计要点：
- 无需登录即可提交（公开端点 POST /public/beta-applications）
- wechat/phone 至少填一个（服务端校验，DB 层均允许 NULL）
- ip_hash 存储客户端 IP 的 SHA256 哈希（不存原始 IP，保护隐私）
- status 状态机：new → contacted → approved/rejected → converted
- feishu_delivery_status 记录飞书通知投递状态（pending/success/failed）
- 索引：status/submitted_at/ip_hash/phone/wechat（支持管理与限流查询）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.beta_application import (
    BETA_APPLICATION_STATUSES_DEFAULT,
)
from app.models._table_meta import table_indexes
from app.models.base import Base


class BetaApplication(Base):
    """内测申请表 - 用户通过公开端点提交的内测申请。

    status 状态机：
    - new: 新申请（默认，等待管理员联系）
    - contacted: 已联系（管理员已沟通）
    - approved: 已通过（同意加入内测）
    - rejected: 已拒绝（不符合条件）
    - converted: 已转化（用户已注册成功）

    飞书投递状态（feishu_delivery_status）：
    - pending: 待投递（Outbox 已写入，等待 worker 处理）
    - success: 投递成功
    - failed: 投递失败（feishu_last_error 记录错误）

    隐私保护：
    - ip_hash 存储 IP 的 SHA256 哈希，不存原始 IP
    - 日志输出时手机号/微信号只显示后 4 位
    - API 响应不返回完整联系方式（仅管理员后台可见）
    """

    __tablename__ = "beta_applications"
    __table_args__ = (
        # status 枚举约束
        CheckConstraint(
            "status IN ('new','contacted','approved','rejected','converted')",
            name="beta_applications_status_check",
        ),
        # reason_code 枚举约束
        CheckConstraint(
            "reason_code IN ('busy','too_many','forget','quant','other')",
            name="beta_applications_reason_code_check",
        ),
        # 索引：支持管理与限流查询
        Index("ix_beta_applications_status", "status"),
        Index("ix_beta_applications_submitted_at", "submitted_at"),
        Index("ix_beta_applications_ip_hash", "ip_hash"),
        Index("ix_beta_applications_phone", "phone"),
        Index("ix_beta_applications_wechat", "wechat"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    wechat: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="微信号（与 phone 至少填一个）"
    )
    phone: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="手机号（与 wechat 至少填一个）"
    )
    watch_stock_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="盯盘股票数量（正整数）"
    )
    reason_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="使用理由代码 busy/too_many/forget/quant/other",
    )
    reason_other: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="补充说明（reason_code='other' 时必填）"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BETA_APPLICATION_STATUSES_DEFAULT,
        server_default=func.text("'new'"),
        comment="new/contacted/approved/rejected/converted",
    )
    source: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="提交来源（如 landing_page/pricing_section）"
    )
    admin_note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="管理员备注"
    )
    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="处理人 user_id（管理员）",
    )
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="处理时间"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="提交时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
    ip_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="客户端 IP 的 SHA256 哈希"
    )
    feishu_delivery_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="飞书投递状态 pending/success/failed",
    )
    feishu_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="飞书投递成功时间"
    )
    feishu_last_error: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="飞书投递最近错误"
    )

    def __repr__(self) -> str:
        return (
            f"<BetaApplication(id={self.id!r}, status={self.status!r}, "
            f"reason_code={self.reason_code!r}, "
            f"watch_stock_count={self.watch_stock_count})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    cols = [c.name for c in BetaApplication.__table__.columns]
    print(f"BetaApplication.__tablename__={BetaApplication.__tablename__}")
    print(f"BetaApplication columns={cols}")

    required = [
        "id", "wechat", "phone", "watch_stock_count", "reason_code",
        "reason_other", "status", "source", "admin_note", "handled_by",
        "handled_at", "submitted_at", "updated_at", "ip_hash",
        "feishu_delivery_status", "feishu_delivered_at", "feishu_last_error",
    ]
    for field in required:
        assert field in cols, f"缺少字段: {field}"

    # 验证索引
    all_indexed_cols: set[str] = set()
    for idx in table_indexes(BetaApplication):
        for col in idx.columns:
            all_indexed_cols.add(col.name)
    for required_col in ["status", "submitted_at", "ip_hash", "phone", "wechat"]:
        assert required_col in all_indexed_cols, f"缺少 {required_col} 索引"

    print(f"indexes={[idx.name for idx in table_indexes(BetaApplication)]}")
    print("OK")
