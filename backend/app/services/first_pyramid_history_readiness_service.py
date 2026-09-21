"""Neutral canonical history-run readiness predicate（consumer-eligibility）。

[REVIEW-BACKEND-FINAL-CLOSURE Phase 5] 从 ``review_bootstrap_service`` 抽离
``validate_canonical_history_run_readiness`` 到本 neutral owner，使 bootstrap
模块可被物理删除而不影响 orchestrator / scope_service 的 history lineage 校验。

本模块只依赖 First Pyramid history 模型与 ``first_pyramid_history_service``，
不依赖任何 bootstrap / replay / review 业务代码，是纯粹的判定逻辑。
"""
from __future__ import annotations

import json as _json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
from app.services.first_pyramid_history_service import (
    ALLOWED_NON_BLOCKING_SKIP_CATEGORIES,
    classify_history_skip_reason,
)

CANONICAL_HISTORY_RUN_SCOPE = "all_a_share"
_NON_TERMINAL_ITEM_STATUSES = ("pending", "running")


async def validate_canonical_history_run_readiness(
    session: AsyncSession,
    run_id: uuid.UUID,
    required_history_contract_version: str,
    required_trade_date: date | None = None,
) -> dict[str, Any]:
    """CANONICAL_HISTORY_RUN_READY predicate。

    [CHANGE-20260809] Phase 4B.1：把 Stage B 的 canonical 判定从
    ``run.status == 'succeeded'`` 改为显式 consumer-eligibility contract。

    背景：``_derive_run_final_status`` 只在 ``skipped == 0`` 时返回 succeeded，
    因此任何存在合法 skip（历史不足 / 无日线数据）的 Stage A run 永久是 ``partial``，
    却仍然可能是完全正确的 canonical source。

    ``HistoryRun.status`` 表达 **execution outcome**；
    canonical readiness 表达 **consumption eligibility**。两者是不同概念，
    因此本函数不修改 ``_derive_run_final_status``，只定义消费侧判定。

    predicate 要求（全部满足才 ready）：

    A. run exists 且 ``scope == 'all_a_share'``
    B. ``metadata_json.history_contract_version == required``
    C. terminal：无 pending / running run item
    D. ``failed_count == 0`` 且无 failed run item
    E. count reconciliation：``expected == succeeded + skipped``
    F. ``succeeded_count > 0``
    G. SUCCESS_SET == CANONICAL_STATE_SET：每个 succeeded run item 都至少有一条
       ``source_history_run_id == run.id`` 且 contract 匹配的 daily state
    H. 所有 skip reason 都属于已知 non-blocking category（UNKNOWN → reject）

    [HISTORY-CURRENT-DATE-LIFECYCLE-01 §9/§11] 新增可选 predicate：

    I. 当 ``required_trade_date`` 非 None 时，
       ``TARGET_DATE_ELIGIBLE_SET == TARGET_DATE_STATE_SET``，其中

       - ELIGIBLE = canonical SUCCESS_SET ∩ 在 required_trade_date 有 completed daily bar
       - STATE    = ``source_history_run_id == run.id`` ∧ contract 匹配
                    ∧ ``trade_date == required_trade_date``

       刻意**不使用** ``MAX(trade_date)``，也**不使用** ``target rows > 0``：
       前者无法发现部分 instrument 缺 target state，后者会让 1 行冒充全量覆盖。
       停牌/退市（target date 无 completed bar）不要求 target state，因此不会误判 not_ready。

    ``required_trade_date=None``（默认）时行为与扩展前完全一致（backward compatible）。

    任何一项不满足 → ``{status:'not_ready', reason: ...}``（fail closed）。
    """
    from app.models.bar import BarDaily

    def _not_ready(reason: str) -> dict[str, Any]:
        return {"status": "not_ready", "reason": reason}

    # --- A. run exists + scope ---------------------------------------------
    run = (
        await session.execute(
            select(FirstPyramidHistoryRun).where(FirstPyramidHistoryRun.id == run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return _not_ready("history_source_run_not_found")
    if run.scope != CANONICAL_HISTORY_RUN_SCOPE:
        return _not_ready(f"history_source_run_wrong_scope:{run.scope}")

    # --- B. contract（pre-v2 / NULL contract 必须继续被拒绝）-----------------
    meta: dict[str, Any] = {}
    if isinstance(run.metadata_json, str) and run.metadata_json:
        try:
            parsed = _json.loads(run.metadata_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    elif isinstance(run.metadata_json, dict):
        meta = run.metadata_json
    run_contract = meta.get("history_contract_version")
    if run_contract != required_history_contract_version:
        return _not_ready(f"history_source_run_wrong_contract:{run_contract}")

    # --- C/D. terminal + no failure（以 run item 实况为准，不信 counter）------
    status_rows = (
        await session.execute(
            select(
                FirstPyramidHistoryRunItem.status,
                func.count(),
            )
            .where(FirstPyramidHistoryRunItem.history_run_id == run_id)
            .group_by(FirstPyramidHistoryRunItem.status)
        )
    ).all()
    item_counts = {str(row[0]): int(row[1]) for row in status_rows}

    for non_terminal in _NON_TERMINAL_ITEM_STATUSES:
        if item_counts.get(non_terminal, 0) > 0:
            return _not_ready(
                f"history_source_run_not_terminal:{non_terminal}="
                f"{item_counts[non_terminal]}"
            )

    failed_items = item_counts.get("failed", 0)
    if failed_items > 0 or int(run.failed_count or 0) > 0:
        return _not_ready(
            f"history_source_run_has_failures:items={failed_items},"
            f"counter={int(run.failed_count or 0)}"
        )

    # --- E. count reconciliation -------------------------------------------
    expected_count = int(run.expected_count or 0)
    succeeded_count = int(run.succeeded_count or 0)
    skipped_count = int(run.skipped_count or 0)
    if expected_count != succeeded_count + skipped_count:
        return _not_ready(
            "history_source_run_count_mismatch:"
            f"expected={expected_count},succeeded={succeeded_count},"
            f"skipped={skipped_count}"
        )

    # --- F. 至少有一个 succeeded ---------------------------------------------
    if succeeded_count <= 0:
        return _not_ready("history_source_run_no_succeeded_items")

    # --- H. skip reason 白名单（UNKNOWN → reject）----------------------------
    if skipped_count > 0:
        skip_rows = (
            await session.execute(
                select(FirstPyramidHistoryRunItem.last_error)
                .where(FirstPyramidHistoryRunItem.history_run_id == run_id)
                .where(FirstPyramidHistoryRunItem.status == "skipped")
            )
        ).all()
        unknown_reasons: set[str] = set()
        for row in skip_rows:
            category = classify_history_skip_reason(row[0])
            if category not in ALLOWED_NON_BLOCKING_SKIP_CATEGORIES:
                unknown_reasons.add((row[0] or "").strip()[:80] or "<empty>")
        if unknown_reasons:
            sample = ",".join(sorted(unknown_reasons)[:3])
            return _not_ready(
                f"history_source_run_unknown_skip_reason:{sample}"
            )

    # --- G. SUCCESS_SET == CANONICAL_STATE_SET ------------------------------
    success_instruments = select(
        FirstPyramidHistoryRunItem.instrument_id
    ).where(
        FirstPyramidHistoryRunItem.history_run_id == run_id,
        FirstPyramidHistoryRunItem.status == "succeeded",
    )
    canonical_instruments = select(
        FirstPyramidHistoryDailyState.instrument_id
    ).where(
        FirstPyramidHistoryDailyState.source_history_run_id == run_id,
        FirstPyramidHistoryDailyState.history_contract_version
        == required_history_contract_version,
    )
    missing_state_count = (
        await session.execute(
            select(func.count()).select_from(
                success_instruments.except_(canonical_instruments).subquery()
            )
        )
    ).scalar_one()
    if int(missing_state_count or 0) > 0:
        return _not_ready(
            f"history_source_run_success_state_mismatch:missing={int(missing_state_count)}"
        )

    # --- I. TARGET_DATE_ELIGIBLE_SET == TARGET_DATE_STATE_SET ---------------
    # [HISTORY-CURRENT-DATE-LIFECYCLE-01 §9/§11] 只在 caller 显式要求 target date 时生效。
    target_date_eligible_count: int | None = None
    target_date_state_count: int | None = None
    if required_trade_date is not None:
        # ELIGIBLE = SUCCESS_SET ∩ 在 required_trade_date 有 completed daily bar
        # （停牌/退市 instrument 当日无 bar → 不进 ELIGIBLE → 不要求 target state）
        target_eligible = (
            select(FirstPyramidHistoryRunItem.instrument_id)
            .join(
                BarDaily,
                BarDaily.instrument_id == FirstPyramidHistoryRunItem.instrument_id,
            )
            .where(
                FirstPyramidHistoryRunItem.history_run_id == run_id,
                FirstPyramidHistoryRunItem.status == "succeeded",
                BarDaily.trade_date == required_trade_date,
                BarDaily.close.is_not(None),
            )
        )
        target_state = select(FirstPyramidHistoryDailyState.instrument_id).where(
            FirstPyramidHistoryDailyState.source_history_run_id == run_id,
            FirstPyramidHistoryDailyState.history_contract_version
            == required_history_contract_version,
            FirstPyramidHistoryDailyState.trade_date == required_trade_date,
        )

        target_date_eligible_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_eligible.distinct().subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        target_date_state_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_state.distinct().subquery()
                    )
                )
            ).scalar_one()
            or 0
        )

        # 双向差集：既拒绝缺 target state，也拒绝多出不该有的 target state
        missing_target = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_eligible.except_(target_state).subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        extra_target = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_state.except_(target_eligible).subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        if missing_target > 0 or extra_target > 0:
            return _not_ready(
                "history_source_run_target_date_state_mismatch:"
                f"date={required_trade_date.isoformat()},"
                f"eligible={target_date_eligible_count},"
                f"state={target_date_state_count},"
                f"missing={missing_target},extra={extra_target}"
            )

    result: dict[str, Any] = {
        "status": "ok",
        "run_id": run_id,
        "expected_count": expected_count,
        "succeeded_count": succeeded_count,
        "skipped_count": skipped_count,
        "run_status": run.status,
    }
    if required_trade_date is not None:
        result["required_trade_date"] = required_trade_date.isoformat()
        result["target_date_eligible_count"] = target_date_eligible_count
        result["target_date_state_count"] = target_date_state_count
    return result
