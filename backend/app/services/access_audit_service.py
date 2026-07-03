"""访问审计日志服务 - 统一记录与查询 admin 关键操作。

提供：
- write_audit_log: 写入审计日志（不 commit，由调用方控制事务）
- query_audit_logs: 查询审计日志（支持多条件筛选 + 分页）

设计要点：
- write_audit_log 不 commit：保证审计日志与业务操作在同一事务内原子提交
  （业务操作失败回滚时，审计日志一并回滚，避免出现"有日志无业务结果"的脏数据）
- 调用方应在业务操作成功后、commit 前调用 write_audit_log
- query_audit_logs 按 created_at 降序返回（最新优先），支持 actor / target /
  action / 时间范围筛选 + limit/offset 分页

接入位置：
- admin 端点（admin_subscription.py 等）在业务操作后调用 write_audit_log
- 后续可扩展 admin 审计日志查询端点（基于 query_audit_logs）

字段约定（与 docs/安全规范.md 8.1 节关键操作日志对齐）：
- action: "<target_type>.<verb>"，如 invite_code.create / invite_code.revoke
- target_type: invite_code / user / subscription 等
- target_id: 目标对象 ID 字符串（UUID 转字符串）
- before_data / after_data: 操作前后状态快照（dict，JSONB 存储）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_audit_log import AccessAuditLog

# 默认分页大小（与 admin 端点现有 list 查询保持一致）
_DEFAULT_LIMIT = 50
# 单页最大返回数（防止超大查询）
_MAX_LIMIT = 200


async def write_audit_log(
    db: AsyncSession,
    actor_user_id: UUID,
    action: str,
    target_type: str,
    target_id: str | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    request_id: str | None = None,
    ip_hash: str | None = None,
) -> AccessAuditLog:
    """写入审计日志（不 commit，由调用方控制事务）。

    Args:
        db: 异步数据库会话
        actor_user_id: 操作者 user_id（admin）
        action: 操作类型，约定格式 "<target_type>.<verb>"
            （如 invite_code.create / invite_code.revoke）
        target_type: 目标对象类型（如 invite_code / user / subscription）
        target_id: 目标对象 ID 字符串（UUID 转字符串，可空）
        before_data: 操作前状态快照（可空）
        after_data: 操作后状态快照（可空）
        request_id: 请求追踪 ID（可空）
        ip_hash: IP 哈希（可空，不存明文 IP）

    Returns:
        已写入的 AccessAuditLog 对象（已 flush，未 commit）

    注意：
        - 本函数不调用 db.commit()，由调用方在业务事务边界统一 commit
        - flush 后对象 id 与 server_default 字段可用
    """
    log = AccessAuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before_data=before_data,
        after_data=after_data,
        request_id=request_id,
        ip_hash=ip_hash,
    )
    db.add(log)
    await db.flush()
    return log


async def query_audit_logs(
    db: AsyncSession,
    actor_user_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> list[AccessAuditLog]:
    """查询审计日志（支持多条件筛选 + 分页）。

    Args:
        db: 异步数据库会话
        actor_user_id: 按操作者筛选（可空）
        target_type: 按目标类型筛选（可空）
        target_id: 按目标 ID 筛选（可空，通常与 target_type 同时传入）
        action: 按操作类型筛选（可空）
        start_time: 起始时间（含，按 created_at >= start_time 筛选）
        end_time: 截止时间（含，按 created_at <= end_time 筛选）
        limit: 分页大小，默认 50，上限 200
        offset: 分页偏移，默认 0

    Returns:
        审计日志列表，按 created_at 降序（最新优先）

    Raises:
        ValueError: limit <= 0 或 offset < 0
    """
    if limit <= 0:
        raise ValueError(f"limit 必须 > 0，实际: {limit}")
    if offset < 0:
        raise ValueError(f"offset 必须 >= 0，实际: {offset}")
    # 限制单页最大返回数，防止超大查询拖慢数据库
    limit = min(limit, _MAX_LIMIT)

    # 构建筛选条件（仅追加非空条件，保持查询效率）
    stmt = select(AccessAuditLog)
    if actor_user_id is not None:
        stmt = stmt.where(AccessAuditLog.actor_user_id == actor_user_id)
    if target_type is not None:
        stmt = stmt.where(AccessAuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AccessAuditLog.target_id == target_id)
    if action is not None:
        stmt = stmt.where(AccessAuditLog.action == action)
    if start_time is not None:
        stmt = stmt.where(AccessAuditLog.created_at >= start_time)
    if end_time is not None:
        stmt = stmt.where(AccessAuditLog.created_at <= end_time)

    # 按 created_at 降序（最新优先），与 admin 列表端点排序惯例一致
    stmt = stmt.order_by(AccessAuditLog.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(stmt)
    return list(result.scalars().all())


if __name__ == "__main__":
    # [AuditLog] - 描述: 自测入口，验证函数签名与默认值（无副作用，不连接数据库）
    import inspect

    write_sig = inspect.signature(write_audit_log)
    query_sig = inspect.signature(query_audit_logs)

    # write_audit_log 必填参数
    write_required = {
        name
        for name, p in write_sig.parameters.items()
        if p.default is inspect.Parameter.empty and name != "db"
    }
    assert write_required == {"actor_user_id", "action", "target_type"}, (
        f"write_audit_log 必填参数不匹配: {write_required}"
    )

    # query_audit_logs 默认 limit/offset
    assert query_sig.parameters["limit"].default == _DEFAULT_LIMIT
    assert query_sig.parameters["offset"].default == 0

    print(f"write_audit_log params={list(write_sig.parameters)}")
    print(f"query_audit_logs params={list(query_sig.parameters)}")
    print(f"_DEFAULT_LIMIT={_DEFAULT_LIMIT}, _MAX_LIMIT={_MAX_LIMIT}")
    print("OK: access_audit_service 函数签名验证通过")
