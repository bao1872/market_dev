"""V2.1 邀请码兑换 API 路由 - 用户提交邀请码兑换能力授权。

端点：
- POST /auth/redeem-v2: 已认证用户兑换 V2.1 邀请码，按能力创建 grant

设计说明（PRD §6.2 + §7 + Phase E5 错误合同）：
- 需要有效 access token（登录状态）
- 调用 redeem_invite_code_with_capabilities 原子事务
- 错误合同：
  - 400: 邀请码无效 / usage_type 非法
  - 409 INVITE_CODE_ALREADY_REDEEMED: 邀请码已被兑换
  - 409 INVITE_CODE_REVOKED: 邀请码已被撤销
- 成功返回 RedeemV2Response（invite_code_id + redeemed_at + grants）
- 兑换后用户 AccessContext 缓存被精确失效（service 层已处理）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import (
    REASON_INVITE_CODE_ALREADY_REDEEMED,
    REASON_INVITE_CODE_REVOKED,
)
from app.core.deps import get_current_active_user
from app.db import get_db
from app.models.user import User
from app.schemas.invite_capability import (
    RedeemV2GrantItem,
    RedeemV2Request,
    RedeemV2Response,
)
from app.services.invite_capability_service import redeem_invite_code_with_capabilities

router = APIRouter(tags=["auth"])
logger = logging.getLogger("api.redeem_v2")


def _map_redeem_error(exc: ValueError) -> HTTPException:
    """将 redeem_invite_code_with_capabilities 的 ValueError 映射为 HTTP 错误合同（Phase E5）。

    - "邀请码已被兑换" → 409 INVITE_CODE_ALREADY_REDEEMED
    - "邀请码已被撤销" → 409 INVITE_CODE_REVOKED
    - 其他（无效/状态非法/usage_type 非法）→ 400
    """
    msg = str(exc)
    if "已被兑换" in msg:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason_code": REASON_INVITE_CODE_ALREADY_REDEEMED,
                "message": msg,
            },
        )
    if "已被撤销" in msg:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason_code": REASON_INVITE_CODE_REVOKED,
                "message": msg,
            },
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=msg,
    )


@router.post("/auth/redeem-v2", response_model=RedeemV2Response)
async def redeem_invite_code_v2(
    payload: RedeemV2Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> RedeemV2Response:
    """V2.1 邀请码兑换 - 已认证用户提交邀请码，按能力创建 grant。

    PRD §6.2 单码单次 + §7 多次兑换独立续期：
    - 单码 redeemed_at IS NOT NULL 后不可再兑换（409 INVITE_CODE_ALREADY_REDEEMED）
    - 用户可兑换多码，每次创建新 grant
    - 已有能力按 base = max(now, latest_expires_at) 延长
    - 新能力立即生效

    Args:
        payload: 兑换请求（invite_code）
        current_user: 当前用户（由 get_current_active_user 注入）
        db: 异步数据库会话

    Returns:
        RedeemV2Response（invite_code_id + redeemed_at + grants）

    Raises:
        HTTPException 400: 邀请码无效
        HTTPException 409: 邀请码已被兑换 / 已被撤销
    """
    try:
        invite, grants, _capabilities = await redeem_invite_code_with_capabilities(
            db=db,
            raw_invite_code=payload.invite_code,
            user_id=current_user.id,
            usage_type="renewal",
        )
    except ValueError as e:
        raise _map_redeem_error(e) from e

    await db.commit()

    logger.info(
        "V2.1 invite code redeemed: user_id=%s, invite_code_id=%s, grants=%d",
        current_user.id,
        invite.id,
        len(grants),
    )

    return RedeemV2Response(
        invite_code_id=invite.id,
        redeemed_at=invite.redeemed_at,  # type: ignore[arg-type]
        grants=[
            RedeemV2GrantItem(
                capability_key=g.capability_key,
                limit_value=g.limit_value,
                starts_at=g.starts_at,
                expires_at=g.expires_at,
            )
            for g in grants
        ],
    )
