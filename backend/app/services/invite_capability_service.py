"""V2.1 邀请码能力配置服务 - 管理员创建/列表/撤销。

PRD §6 邀请码能力配置 + §8.1 invite_codes 状态推导。

提供：
- create_invite_codes_with_capabilities: 创建邀请码 + 能力配置（事务）
- list_invite_codes_with_capabilities: 列表查询（含能力配置）
- revoke_invite_code_v2: 撤销邀请码（设置 revoked_at）
- get_invite_code_with_capabilities: 查询单个邀请码 + 能力配置

设计原则：
- 单码可包含 1-3 项能力任意组合（PRD §6.4）
- watchlist_management 必须提供 limit_value（正整数）（PRD §6.4）
- 其他能力 limit_value = NULL
- duration_months 必须提供（1 <= x <= MAX_DURATION_MONTHS）（PRD §6.3）
- 邀请码状态由字段推导：revoked_at IS NULL AND redeemed_at IS NULL = available
- 撤销仅允许 available 状态（已兑换不可撤销）（PRD §8.1）
- 明文邀请码仅在创建时返回，后续不可获取（与 V1 一致）

不实现：
- 兑换逻辑（Phase D 的 redeem_invite_code_with_capabilities）
- 自选额度并发控制（Phase F）
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import (
    ALL_CAPABILITY_KEYS,
    MAX_DURATION_MONTHS,
    MAX_WATCHLIST_STOCK_LIMIT,
    WATCHLIST_MANAGEMENT,
)
from app.models.capability_grant import InviteCodeCapability
from app.models.invitation import InviteCode
from app.services.subscription_service import hash_invite_code

__all__ = [
    "InviteCodeCapabilityInput",
    "create_invite_codes_with_capabilities",
    "list_invite_codes_with_capabilities",
    "revoke_invite_code_v2",
    "get_invite_code_with_capabilities",
    "derive_invite_code_status_v2",
    "INVITE_CODE_CHARS",
    "INVITE_CODE_GROUPS",
    "INVITE_CODE_GROUP_LEN",
]

# 邀请码生成参数（与 V1 _generate_invite_code 保持一致）
INVITE_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 排除易混淆字符
INVITE_CODE_GROUPS = 4
INVITE_CODE_GROUP_LEN = 4


class InviteCodeCapabilityInput:
    """邀请码能力配置输入（PRD §6.4）。

    Attributes:
        capability_key: 能力键（必须在 ALL_CAPABILITY_KEYS 中）
        limit_value: 自选额度（仅 watchlist_management 必须正整数；其他能力必须 None）
    """

    def __init__(self, capability_key: str, limit_value: int | None = None) -> None:
        if capability_key not in ALL_CAPABILITY_KEYS:
            raise ValueError(
                f"capability_key 必须在 {ALL_CAPABILITY_KEYS} 中，当前={capability_key!r}"
            )
        if capability_key == WATCHLIST_MANAGEMENT:
            if limit_value is None or limit_value <= 0:
                raise ValueError(
                    f"watchlist_management 必须提供正整数 limit_value，当前={limit_value}"
                )
            if limit_value > MAX_WATCHLIST_STOCK_LIMIT:
                raise ValueError(
                    f"limit_value 超过技术安全上限 {MAX_WATCHLIST_STOCK_LIMIT}，当前={limit_value}"
                )
        else:
            if limit_value is not None:
                raise ValueError(
                    f"非 watchlist_management 能力 limit_value 必须为 None，"
                    f"capability_key={capability_key!r}, limit_value={limit_value}"
                )
        self.capability_key = capability_key
        self.limit_value = limit_value


def _generate_invite_code() -> str:
    """生成随机邀请码明文（XXXX-XXXX-XXXX-XXXX）。"""
    groups = []
    for _ in range(INVITE_CODE_GROUPS):
        group = "".join(
            secrets.choice(INVITE_CODE_CHARS) for _ in range(INVITE_CODE_GROUP_LEN)
        )
        groups.append(group)
    return "-".join(groups)


def derive_invite_code_status_v2(invite: InviteCode) -> str:
    """PRD §8.1 邀请码状态推导（V2.1）。

    - revoked: revoked_at IS NOT NULL
    - redeemed: redeemed_at IS NOT NULL（或旧 used_at IS NOT NULL）
    - available: revoked_at IS NULL AND redeemed_at IS NULL AND used_at IS NULL

    Args:
        invite: InviteCode ORM 对象

    Returns:
        状态字符串：available / redeemed / revoked
    """
    if invite.revoked_at is not None:
        return "revoked"
    # 兼容旧字段：redeemed_at 优先，否则 used_at
    redeemed_at = invite.redeemed_at if hasattr(invite, "redeemed_at") else None
    used_at = invite.used_at if hasattr(invite, "used_at") else None
    if redeemed_at is not None or used_at is not None:
        return "redeemed"
    return "available"


async def create_invite_codes_with_capabilities(
    db: AsyncSession,
    count: int,
    created_by: uuid.UUID,
    duration_months: int,
    capabilities: list[InviteCodeCapabilityInput],
    note: str | None = None,
) -> list[tuple[InviteCode, str]]:
    """创建邀请码 + 能力配置（PRD §6）。

    Args:
        db: 异步数据库会话
        count: 生成数量（1-100）
        created_by: 创建者 user_id（管理员）
        duration_months: 授权月数（1 <= x <= MAX_DURATION_MONTHS）
        capabilities: 能力配置列表（1-3 项）
        note: 批次备注

    Returns:
        list of (InviteCode ORM 对象, 明文邀请码) 元组

    Raises:
        ValueError: 参数非法
    """
    if count < 1 or count > 100:
        raise ValueError(f"count 必须 1-100，当前={count}")
    if duration_months < 1 or duration_months > MAX_DURATION_MONTHS:
        raise ValueError(
            f"duration_months 必须 1-{MAX_DURATION_MONTHS}，当前={duration_months}"
        )
    if not capabilities:
        raise ValueError("capabilities 不能为空（至少 1 项能力）")
    if len(capabilities) > len(ALL_CAPABILITY_KEYS):
        raise ValueError(
            f"capabilities 数量超过能力键总数 {len(ALL_CAPABILITY_KEYS)}"
        )
    # 检查能力键不重复
    keys = [c.capability_key for c in capabilities]
    if len(set(keys)) != len(keys):
        raise ValueError(f"capabilities 存在重复能力键: {keys}")

    results: list[tuple[InviteCode, str]] = []
    for _ in range(count):
        raw_code = _generate_invite_code()
        code_hash = hash_invite_code(raw_code)
        invite = InviteCode(
            code_hash=code_hash,
            status="unused",  # 旧字段保留（V1 兼容）
            grant_days=30,  # 旧字段默认值（V1 兼容）
            plan_code=None,  # V2.1 不再绑定 plan_code
            monitor_limit=None,  # V2.1 不再绑定 monitor_limit
            grant_months=None,  # V2.1 用 duration_months
            duration_months=duration_months,
            note=note,
            created_by=created_by,
            # V2.1 新字段默认 None
            revoked_at=None,
            redeemed_by_user_id=None,
            redeemed_at=None,
        )
        db.add(invite)
        await db.flush()  # 获取 invite.id

        # 创建能力配置
        for cap_input in capabilities:
            capability = InviteCodeCapability(
                invite_code_id=invite.id,
                capability_key=cap_input.capability_key,
                limit_value=cap_input.limit_value,
            )
            db.add(capability)

        results.append((invite, raw_code))

    await db.flush()
    return results


async def get_invite_code_with_capabilities(
    db: AsyncSession,
    invite_code_id: uuid.UUID,
) -> tuple[InviteCode, list[InviteCodeCapability]] | None:
    """查询单个邀请码 + 能力配置。

    Args:
        db: 异步数据库会话
        invite_code_id: 邀请码 ID

    Returns:
        (InviteCode, list[InviteCodeCapability]) 或 None（不存在）
    """
    invite_result = await db.execute(
        select(InviteCode).where(InviteCode.id == invite_code_id)
    )
    invite = invite_result.scalar_one_or_none()
    if invite is None:
        return None

    cap_result = await db.execute(
        select(InviteCodeCapability)
        .where(InviteCodeCapability.invite_code_id == invite_code_id)
        .order_by(InviteCodeCapability.capability_key)
    )
    capabilities = list(cap_result.scalars().all())
    return invite, capabilities


async def list_invite_codes_with_capabilities(
    db: AsyncSession,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[InviteCode, list[InviteCodeCapability]]], int]:
    """查询邀请码列表（含能力配置）。

    Args:
        db: 异步数据库会话
        status_filter: 状态筛选（available/redeemed/revoked）；None=全部
            注：V2.1 状态由字段推导，需要在 Python 中过滤
        limit: 分页大小
        offset: 分页偏移

    Returns:
        (items, total) 元组
        items: list of (InviteCode, list[InviteCodeCapability])
        total: 总数（按筛选条件）
    """
    # 查询所有邀请码（按创建时间倒序）
    stmt = (
        select(InviteCode)
        .order_by(InviteCode.created_at.desc())
    )
    result = await db.execute(stmt)
    all_invites = list(result.scalars().all())

    # 在 Python 中按状态过滤（V2.1 状态推导）
    if status_filter is not None:
        filtered = [
            inv for inv in all_invites
            if derive_invite_code_status_v2(inv) == status_filter
        ]
    else:
        filtered = all_invites

    total = len(filtered)
    page = filtered[offset : offset + limit]

    # 批量查询能力配置
    if not page:
        return [], total

    invite_ids = [inv.id for inv in page]
    cap_stmt = (
        select(InviteCodeCapability)
        .where(InviteCodeCapability.invite_code_id.in_(invite_ids))
        .order_by(
            InviteCodeCapability.invite_code_id,
            InviteCodeCapability.capability_key,
        )
    )
    cap_result = await db.execute(cap_stmt)
    all_caps = list(cap_result.scalars().all())

    # 按 invite_code_id 分组
    caps_by_invite: dict[uuid.UUID, list[InviteCodeCapability]] = {}
    for cap in all_caps:
        caps_by_invite.setdefault(cap.invite_code_id, []).append(cap)

    items = [(inv, caps_by_invite.get(inv.id, [])) for inv in page]
    return items, total


async def revoke_invite_code_v2(
    db: AsyncSession,
    invite_code_id: uuid.UUID,
) -> InviteCode:
    """撤销邀请码（PRD §8.1）。

    规则：
    - 仅 available 状态可撤销（已兑换/已撤销不可撤销）
    - 设置 revoked_at = now
    - 不修改旧 status 字段（V1 兼容）

    Args:
        db: 异步数据库会话
        invite_code_id: 邀请码 ID

    Returns:
        更新后的 InviteCode

    Raises:
        ValueError: 邀请码不存在或状态非 available
    """
    result = await db.execute(
        select(InviteCode).where(InviteCode.id == invite_code_id)
    )
    invite = result.scalar_one_or_none()
    if invite is None:
        raise ValueError(f"邀请码不存在: {invite_code_id}")

    status_v2 = derive_invite_code_status_v2(invite)
    if status_v2 != "available":
        raise ValueError(
            f"仅 available 状态可撤销，当前状态: {status_v2}"
        )

    invite.revoked_at = datetime.now(UTC)
    await db.flush()
    return invite


if __name__ == "__main__":
    # [InviteCapabilityService] - 描述: 自测入口，验证函数签名与 InviteCodeCapabilityInput 校验
    assert callable(create_invite_codes_with_capabilities)
    assert callable(list_invite_codes_with_capabilities)
    assert callable(revoke_invite_code_v2)
    assert callable(get_invite_code_with_capabilities)
    assert callable(derive_invite_code_status_v2)

    # InviteCodeCapabilityInput 校验
    # 1. 合法 watchlist_management
    cap1 = InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=30)
    assert cap1.capability_key == WATCHLIST_MANAGEMENT
    assert cap1.limit_value == 30

    # 2. 合法 market_screening（limit_value=None）
    from app.constants.capability_keys import MARKET_SCREENING, REVIEW_MANAGEMENT

    cap2 = InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=None)
    assert cap2.limit_value is None

    cap3 = InviteCodeCapabilityInput(REVIEW_MANAGEMENT, limit_value=None)
    assert cap3.limit_value is None

    # 3. 非法 capability_key
    try:
        InviteCodeCapabilityInput("invalid_key")
        raise AssertionError("非法 capability_key 应拒绝")
    except ValueError:
        pass

    # 4. watchlist_management 缺少 limit_value
    try:
        InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=None)
        raise AssertionError("watchlist_management 缺少 limit_value 应拒绝")
    except ValueError:
        pass

    # 5. watchlist_management limit_value=0
    try:
        InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=0)
        raise AssertionError("watchlist_management limit_value=0 应拒绝")
    except ValueError:
        pass

    # 6. watchlist_management limit_value 超过上限
    try:
        InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=MAX_WATCHLIST_STOCK_LIMIT + 1)
        raise AssertionError("limit_value 超过上限应拒绝")
    except ValueError:
        pass

    # 7. 非 watchlist 能力带 limit_value
    try:
        InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=30)
        raise AssertionError("非 watchlist 能力带 limit_value 应拒绝")
    except ValueError:
        pass

    # 邀请码生成
    code = _generate_invite_code()
    assert len(code) == INVITE_CODE_GROUPS * (INVITE_CODE_GROUP_LEN + 1) - 1
    assert code.count("-") == INVITE_CODE_GROUPS - 1

    print("InviteCodeCapabilityInput 校验全部通过")
    print(f"生成邀请码示例: {code}")
    print("OK: invite_capability_service 函数签名与校验验证通过")
