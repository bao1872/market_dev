"""选股组合运行服务（C4）- 幂等运行选股方案并写入结果与证据。

核心职责：
- run_selection_plan: 执行选股方案（幂等），写入运行记录 + 结果 + 证据
- preview_selection_plan: 预览结果（不落库），返回数量、样本和成员命中统计

run_selection_plan 流程：
1. 加载方案 + 当前 revision + members + conditions
2. 计算 input_run_set_hash（成员策略版本集指纹）
3. 计算幂等键 = sha256(revision_id + trade_date + trigger_kind + input_run_set_hash)[:16]
4. 检查幂等键是否已存在（幂等：已存在则直接返回已有运行）
5. 创建运行记录（status=running）
6. 执行每个成员（C2 execute_member）
7. 应用 missing_member_policy（FAIL_CLOSED/IGNORE_MEMBER）
8. 组合（C3 ALL/ANY）
9. 排名（C3 白名单表达式）
10. 写入结果 + 证据
11. 更新运行状态（succeeded/failed）

幂等设计：
- 幂等键 = sha256(revision_id + trade_date + trigger_kind + input_run_set_hash)[:16]
- 相同幂等键的运行不重复执行（unique 约束 + 业务层查询双重保障）
- trigger_kind 为运行时参数（不持久化为列，参与幂等键计算）

missing_member_policy：
- FAIL_CLOSED: 任一成员无结果（NO_RESULT/DATA_MISSING）时运行失败
- IGNORE_MEMBER: 无结果的成员被忽略（排除出组合运算）

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.selection_plan import (
    SelectionPlan,
    SelectionPlanMember,
    SelectionPlanRevision,
)
from app.models.selection_plan_run import (
    SelectionPlanResult,
    SelectionPlanRun,
    SelectionResultEvidence,
)
from app.schemas.selection_plan import (
    SelectionPlanPreviewResponse,
    SelectionPlanResultResponse,
)
from app.services.selection_composer import (
    RankedInstrument,
    compose,
    get_contributing_members,
    rank,
)
from app.services.selection_executor import (
    REASON_NO_RESULT,
    MemberMatch,
    execute_member,
)

logger = logging.getLogger("selection_run_service")


def _compute_input_run_set_hash(
    members: list[SelectionPlanMember],
) -> str:
    """计算输入运行集哈希（成员策略版本集指纹）。

    对成员的 strategy_version_id 集合排序后哈希，确保相同输入集产生相同哈希。

    Args:
        members: 方案成员列表

    Returns:
        16 字符哈希字符串
    """
    # 收集 (member_id, strategy_version_id) 对，按 member_id 排序
    version_pairs = sorted(
        (str(m.id), str(m.strategy_version_id) if m.strategy_version_id else "None")
        for m in members
    )
    raw = "|".join(f"{mid}:{vid}" for mid, vid in version_pairs)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compute_idempotency_key(
    revision_id: uuid.UUID,
    trade_date: date,
    trigger_kind: str,
    input_run_set_hash: str,
) -> str:
    """计算幂等键。

    幂等键 = sha256(revision_id + trade_date + trigger_kind + input_run_set_hash)[:16]

    Args:
        revision_id: 方案版本 ID
        trade_date: 交易日
        trigger_kind: 触发方式（manual/scheduled/replay）
        input_run_set_hash: 输入运行集哈希

    Returns:
        16 字符幂等键
    """
    raw = f"{revision_id}|{trade_date.isoformat()}|{trigger_kind}|{input_run_set_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _load_revision_with_members(
    db: AsyncSession, revision_id: uuid.UUID
) -> SelectionPlanRevision | None:
    """加载版本及其成员、条件（使用 selectinload 避免 N+1）。"""
    stmt = (
        select(SelectionPlanRevision)
        .options(
            selectinload(SelectionPlanRevision.members).selectinload(
                SelectionPlanMember.conditions
            )
        )
        .where(SelectionPlanRevision.id == revision_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _execute_all_members(
    db: AsyncSession,
    members: list[SelectionPlanMember],
    trade_date: date,
) -> dict[uuid.UUID, dict[uuid.UUID, MemberMatch]]:
    """执行所有启用的成员，返回 member_id → (instrument_id → MemberMatch) 映射。

    Args:
        db: 异步数据库会话
        members: 方案成员列表
        trade_date: 交易日

    Returns:
        member_id → (instrument_id → MemberMatch) 映射

    Raises:
        RuntimeError: 任一成员执行失败时补充上下文后 re-raise
    """
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]] = {}
    for member in members:
        if not member.enabled:
            logger.info("跳过未启用成员: member_id=%s", member.id)
            continue
        try:
            matches = await execute_member(db, member, trade_date)
        except Exception as exc:
            raise RuntimeError(
                f"执行成员失败 member_id={member.id}, "
                f"position={member.position}, trade_date={trade_date}: {exc}"
            ) from exc
        member_matches[member.id] = matches
        logger.info(
            "成员执行完成: member_id=%s, matches=%d",
            member.id, len(matches),
        )
    return member_matches


def _apply_missing_member_policy(
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
    members: list[SelectionPlanMember],
    policy: str,
) -> None:
    """应用成员缺失策略。

    FAIL_CLOSED: 任一启用成员无结果（空 matches）时，标记运行应失败
    IGNORE_MEMBER: 无结果的成员被忽略（已通过空字典自然排除出组合运算）

    Args:
        member_matches: 成员执行结果
        members: 方案成员列表
        policy: 缺失策略 FAIL_CLOSED/IGNORE_MEMBER

    Raises:
        ValueError: FAIL_CLOSED 且有成员无结果时
    """
    if policy == "IGNORE_MEMBER":
        # 忽略无结果成员，无需处理
        return

    # FAIL_CLOSED: 检查是否有启用成员无结果
    for member in members:
        if not member.enabled:
            continue
        matches = member_matches.get(member.id, {})
        if not matches:
            raise ValueError(
                f"成员无结果且策略为 FAIL_CLOSED: member_id={member.id}, "
                f"position={member.position}（策略版本可能未解析或当日无选股结果）"
            )


def _collect_all_instruments(
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
) -> set[uuid.UUID]:
    """收集所有出现过的 instrument_id（含命中与未命中）。"""
    all_ids: set[uuid.UUID] = set()
    for matches in member_matches.values():
        all_ids.update(matches.keys())
    return all_ids


async def run_selection_plan(
    db: AsyncSession,
    plan_id: uuid.UUID,
    trade_date: date,
    trigger_kind: str,
    user_id: uuid.UUID,
) -> SelectionPlanRun:
    """执行选股方案（幂等）。

    流程见模块文档字符串。

    Args:
        db: 异步数据库会话
        plan_id: 方案 ID
        trade_date: 交易日
        trigger_kind: 触发方式（manual/scheduled/replay）
        user_id: 触发用户 ID

    Returns:
        SelectionPlanRun ORM 对象

    Raises:
        ValueError: 方案/版本不存在、FAIL_CLOSED 成员无结果
        RuntimeError: 成员执行或结果写入失败
    """
    # 1. 加载方案 + 当前 revision
    plan_stmt = select(SelectionPlan).where(SelectionPlan.id == plan_id)
    plan_result = await db.execute(plan_stmt)
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise ValueError(f"方案不存在: plan_id={plan_id}")

    # 查询当前 revision
    rev_stmt = select(SelectionPlanRevision).where(
        SelectionPlanRevision.selection_plan_id == plan.id,
        SelectionPlanRevision.revision == plan.current_revision,
    )
    rev_result = await db.execute(rev_stmt)
    revision = rev_result.scalar_one_or_none()
    if revision is None:
        raise ValueError(
            f"方案版本不存在: plan_id={plan_id}, revision={plan.current_revision}"
        )

    # 加载 revision 含 members + conditions
    revision_loaded = await _load_revision_with_members(db, revision.id)
    if revision_loaded is None:
        raise ValueError(f"版本加载失败: revision_id={revision.id}")

    members = list(revision_loaded.members)

    # 2. 计算 input_run_set_hash
    input_run_set_hash = _compute_input_run_set_hash(members)

    # 3. 计算幂等键
    idempotency_key = _compute_idempotency_key(
        revision_loaded.id, trade_date, trigger_kind, input_run_set_hash
    )

    # 4. 检查幂等键是否已存在
    existing_stmt = select(SelectionPlanRun).where(
        SelectionPlanRun.idempotency_key == idempotency_key
    )
    existing_result = await db.execute(existing_stmt)
    existing_run = existing_result.scalar_one_or_none()
    if existing_run is not None:
        logger.info(
            "运行已存在（幂等）: idempotency_key=%s, run_id=%s",
            idempotency_key, existing_run.id,
        )
        return existing_run

    # 5. 创建运行记录（status=running）
    run = SelectionPlanRun(
        user_id=user_id,
        selection_plan_id=plan.id,
        revision_id=revision_loaded.id,
        trade_date=trade_date,
        status="running",
        input_run_set_hash=input_run_set_hash,
        idempotency_key=idempotency_key,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise RuntimeError(
            f"创建运行记录失败 idempotency_key={idempotency_key}: {exc}"
        ) from exc

    try:
        # 6. 执行每个成员（C2）
        member_matches = await _execute_all_members(db, members, trade_date)

        # 7. 应用 missing_member_policy
        _apply_missing_member_policy(
            member_matches, members, revision_loaded.missing_member_policy
        )

        # 8. 组合（C3 ALL/ANY）
        composed_ids = compose(member_matches, revision_loaded.operator)
        logger.info(
            "组合完成: operator=%s, composed=%d",
            revision_loaded.operator, len(composed_ids),
        )

        # 9. 排名（C3 白名单表达式）
        sort_spec = list(revision_loaded.sort_spec) if revision_loaded.sort_spec else []
        ranked_list = rank(composed_ids, member_matches, sort_spec)

        # 10. 写入结果 + 证据
        await _write_results_and_evidence(
            db, run.id, ranked_list, member_matches
        )

        # 11. 更新运行状态
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"更新运行状态失败 run_id={run.id}: {exc}"
            ) from exc

        logger.info(
            "运行成功: run_id=%s, results=%d, composed=%d",
            run.id, len(ranked_list), len(composed_ids),
        )
        return run

    except Exception as exc:
        # 运行失败：更新状态并 re-raise
        logger.error("运行失败 run_id=%s: %s", run.id, exc)
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        try:
            await db.flush()
        except Exception as flush_exc:
            await db.rollback()
            raise RuntimeError(
                f"更新运行失败状态失败 run_id={run.id}: {flush_exc}（原错误: {exc}）"
            ) from flush_exc
        raise


async def _write_results_and_evidence(
    db: AsyncSession,
    run_id: uuid.UUID,
    ranked_list: list[RankedInstrument],
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
) -> None:
    """写入运行结果 + 证据链。

    为每个 ranked instrument 创建：
    - SelectionPlanResult: 最终命中结果（matched=True, rank_value, matched_member_ids）
    - SelectionResultEvidence: 每个成员对该标的的证据

    同时为未命中但出现过的标的创建结果（matched=False），保留证据链。

    Args:
        db: 异步数据库会话
        run_id: 运行 ID
        ranked_list: 排名后的标的列表
        member_matches: 成员执行结果

    Raises:
        RuntimeError: 写入失败时补充上下文后 re-raise
    """
    # 收集所有出现过的 instrument_id（含命中与未命中）
    all_instrument_ids = _collect_all_instruments(member_matches)
    ranked_ids = {r.instrument_id for r in ranked_list}
    # 未命中但出现过的标的
    unmatched_ids = all_instrument_ids - ranked_ids

    # 写入命中的结果
    ranked_map = {r.instrument_id: r for r in ranked_list}
    try:
        for iid in all_instrument_ids:
            ranked = ranked_map.get(iid)
            matched = iid in ranked_ids
            rank_value = ranked.score if ranked else None
            contributing = (
                ranked.contributing_members if ranked
                else get_contributing_members(iid, member_matches)
            )

            result = SelectionPlanResult(
                plan_run_id=run_id,
                instrument_id=iid,
                matched=matched,
                matched_member_ids=contributing,
                rank_value=rank_value,
                summary=_build_result_summary(iid, member_matches),
            )
            db.add(result)
            await db.flush()

            # 写入证据链：每个成员对该标的的证据
            for member_id, matches in member_matches.items():
                mm = matches.get(iid)
                if mm is None:
                    # 该成员未覆盖此标的（策略无此标的结果）
                    evidence = SelectionResultEvidence(
                        selection_result_id=result.id,
                        member_id=member_id,
                        strategy_result_id=None,
                        matched=False,
                        reason_code=REASON_NO_RESULT,
                        summary={},
                    )
                else:
                    evidence = SelectionResultEvidence(
                        selection_result_id=result.id,
                        member_id=member_id,
                        strategy_result_id=mm.result_id,
                        matched=mm.matched,
                        reason_code=mm.missing_reason,
                        summary=mm.metrics_summary,
                    )
                db.add(evidence)

        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise RuntimeError(
            f"写入结果与证据失败 run_id={run_id}: {exc}"
        ) from exc

    logger.info(
        "写入结果与证据: run_id=%s, results=%d (matched=%d, unmatched=%d)",
        run_id, len(all_instrument_ids), len(ranked_ids), len(unmatched_ids),
    )


def _build_result_summary(
    instrument_id: uuid.UUID,
    member_matches: dict[uuid.UUID, dict[uuid.UUID, MemberMatch]],
) -> dict[str, Any]:
    """构建结果摘要 JSONB。

    包含每个命中成员的关键指标快照。

    Args:
        instrument_id: 标的 ID
        member_matches: 成员执行结果

    Returns:
        摘要字典 {member_id_str: metrics_summary}
    """
    summary: dict[str, Any] = {}
    for member_id, matches in member_matches.items():
        mm = matches.get(instrument_id)
        if mm is not None and mm.matched:
            summary[str(member_id)] = mm.metrics_summary
    return summary


async def preview_selection_plan(
    db: AsyncSession,
    plan_id: uuid.UUID,
    trade_date: date,
    revision_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> SelectionPlanPreviewResponse:
    """预览选股方案结果（不落库）。

    返回数量、样本（最多 20 条）和成员命中统计。

    Args:
        db: 异步数据库会话
        plan_id: 方案 ID
        trade_date: 交易日
        revision_id: 指定版本 ID（None 则用当前版本）
        user_id: 用户 ID

    Returns:
        预览响应（不持久化）

    Raises:
        ValueError: 方案/版本不存在、FAIL_CLOSED 成员无结果
        RuntimeError: 成员执行失败
    """
    # 1. 加载方案
    plan_stmt = select(SelectionPlan).where(SelectionPlan.id == plan_id)
    plan_result = await db.execute(plan_stmt)
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise ValueError(f"方案不存在: plan_id={plan_id}")

    # 2. 加载版本（指定或当前）
    if revision_id is not None:
        revision_loaded = await _load_revision_with_members(db, revision_id)
        if revision_loaded is None:
            raise ValueError(f"版本不存在: revision_id={revision_id}")
    else:
        rev_stmt = select(SelectionPlanRevision).where(
            SelectionPlanRevision.selection_plan_id == plan.id,
            SelectionPlanRevision.revision == plan.current_revision,
        )
        rev_result = await db.execute(rev_stmt)
        revision = rev_result.scalar_one_or_none()
        if revision is None:
            raise ValueError(
                f"方案版本不存在: plan_id={plan_id}, revision={plan.current_revision}"
            )
        revision_loaded = await _load_revision_with_members(db, revision.id)
        if revision_loaded is None:
            raise ValueError(f"版本加载失败: revision_id={revision.id}")

    members = list(revision_loaded.members)

    # 3. 执行每个成员（C2）
    member_matches = await _execute_all_members(db, members, trade_date)

    # 4. 应用 missing_member_policy
    _apply_missing_member_policy(
        member_matches, members, revision_loaded.missing_member_policy
    )

    # 5. 组合（C3 ALL/ANY）
    composed_ids = compose(member_matches, revision_loaded.operator)

    # 6. 排名（C3 白名单表达式）
    sort_spec = list(revision_loaded.sort_spec) if revision_loaded.sort_spec else []
    ranked_list = rank(composed_ids, member_matches, sort_spec)

    # 7. 构建预览响应（不落库）
    sample = [
        SelectionPlanResultResponse(
            id=uuid.uuid4(),  # 预览无真实 ID，生成临时 UUID
            plan_run_id=uuid.uuid4(),  # 预览无真实 run_id
            instrument_id=r.instrument_id,
            matched=True,
            matched_member_ids=r.contributing_members,
            rank_value=r.score,
            summary=_build_result_summary(r.instrument_id, member_matches),
        )
        for r in ranked_list[:20]
    ]

    # 成员命中统计
    member_hit_stats: dict[str, int] = {}
    for member_id, matches in member_matches.items():
        hit_count = sum(1 for mm in matches.values() if mm.matched)
        member_hit_stats[str(member_id)] = hit_count

    return SelectionPlanPreviewResponse(
        total=len(composed_ids),
        sample=sample,
        member_hit_stats=member_hit_stats,
    )


if __name__ == "__main__":
    # 自测入口：验证辅助函数（无副作用，不连接数据库）

    # 测试 _compute_input_run_set_hash
    class MockMember:
        def __init__(self, mid, vid):
            self.id = mid
            self.strategy_version_id = vid

    mid1, mid2 = uuid.uuid4(), uuid.uuid4()
    vid1, vid2 = uuid.uuid4(), uuid.uuid4()
    members_a = [MockMember(mid1, vid1), MockMember(mid2, vid2)]
    members_b = [MockMember(mid2, vid2), MockMember(mid1, vid1)]  # 顺序不同
    hash_a = _compute_input_run_set_hash(members_a)
    hash_b = _compute_input_run_set_hash(members_b)
    assert hash_a == hash_b, f"哈希应与顺序无关: {hash_a} != {hash_b}"
    assert len(hash_a) == 16
    print(f"input_run_set_hash（顺序无关）: {hash_a} ✓")

    # 测试 None strategy_version_id
    members_none = [MockMember(mid1, None), MockMember(mid2, vid2)]
    hash_none = _compute_input_run_set_hash(members_none)
    assert hash_none != hash_a
    print(f"None version_id 哈希不同: {hash_none} ✓")

    # 测试 _compute_idempotency_key
    rev_id = uuid.uuid4()
    td = date(2026, 6, 18)
    key1 = _compute_idempotency_key(rev_id, td, "manual", hash_a)
    key2 = _compute_idempotency_key(rev_id, td, "manual", hash_a)
    key3 = _compute_idempotency_key(rev_id, td, "scheduled", hash_a)
    assert key1 == key2, "相同输入应产生相同幂等键"
    assert key1 != key3, "不同 trigger_kind 应产生不同幂等键"
    assert len(key1) == 16
    print(f"idempotency_key（manual）: {key1} ✓")
    print(f"idempotency_key（scheduled）: {key3} ✓")

    # 测试 _apply_missing_member_policy
    mm1 = {uuid.uuid4(): MemberMatch(uuid.uuid4(), True, {})}
    member_matches_ok = {mid1: mm1, mid2: mm1}
    member_matches_empty = {mid1: mm1, mid2: {}}

    # IGNORE_MEMBER 不应抛异常
    _apply_missing_member_policy(member_matches_empty, [], "IGNORE_MEMBER")
    print("IGNORE_MEMBER 空成员不抛异常 ✓")

    # FAIL_CLOSED 空成员应抛异常
    class MockMemberEnabled:
        def __init__(self, mid, enabled=True):
            self.id = mid
            self.enabled = enabled
            self.position = 0

    try:
        _apply_missing_member_policy(
            member_matches_empty,
            [MockMemberEnabled(mid1), MockMemberEnabled(mid2)],
            "FAIL_CLOSED",
        )
    except ValueError as e:
        print(f"FAIL_CLOSED 空成员抛异常: {e} ✓")

    # 测试 _collect_all_instruments
    iid1, iid2, iid3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    matches = {
        mid1: {iid1: MemberMatch(iid1, True), iid2: MemberMatch(iid2, False)},
        mid2: {iid1: MemberMatch(iid1, True), iid3: MemberMatch(iid3, True)},
    }
    all_ids = _collect_all_instruments(matches)
    assert all_ids == {iid1, iid2, iid3}
    print(f"collect_all_instruments: {all_ids} ✓")

    # 测试 _build_result_summary
    summary = _build_result_summary(iid1, matches)
    assert str(mid1) in summary
    assert str(mid2) in summary
    print(f"build_result_summary: keys={list(summary.keys())} ✓")

    print("OK")
