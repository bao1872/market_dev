"""Review bootstrap 异步作业层（提交 / 执行 / 查询）。

为什么需要这一层：
    120 交易日 × 全 scope 的历史回填不可能在单个 HTTP 请求内同步完成。
    Admin API 只负责**提交** queued 任务并立即返回 job_run_id（202），
    真正的计算由 after-close worker 容器内的 bootstrap poll 领取执行
    （FOR UPDATE SKIP LOCKED + lease_epoch fencing + heartbeat）。

关键契约：
    - dry_run 严格零业务写入：只写 SchedulerJobRun 这一条任务记录，
      不创建 MarketReviewRun、不写 observations、不切 pointer。
    - 审计字段 operator / reason / input_hash 始终记录在 SchedulerJobRun
      metadata（任务审计），但只有 apply 才会落到 MarketReviewRun metadata。
    - 明细通过 status 接口分页返回，避免一次性返回上万条 scope 结果。

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_bootstrap_job_service
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.idempotency_service import acquire_job_run_lock
from app.services.review_bootstrap_service import (
    BOOTSTRAP_ALGORITHM_VERSION,
    DEFAULT_BOOTSTRAP_CHUNK_DAYS,
    DEFAULT_BOOTSTRAP_DAYS,
    DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB,
    MIN_BOOTSTRAP_DAYS,
    SCOPE_COUNT_KEYS,
    bootstrap_history,
    compute_input_hash,
    resolve_bootstrap_end_date,
)

logger = logging.getLogger("review_bootstrap_job_service")

REVIEW_BOOTSTRAP_JOB_NAME = "review_bootstrap"

# 全量回填耗时较长，租约给足；heartbeat 每 30s 续租，超时由 watchdog 回收。
REVIEW_BOOTSTRAP_LEASE_SECONDS = 900

# status 接口单页明细上限，防止一次性返回上万条 scope 结果。
MAX_DETAIL_PAGE_SIZE = 200
DEFAULT_DETAIL_PAGE_SIZE = 50

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "skipped"})
_RESUMABLE_STATUSES = frozenset({"failed", "interrupted"})


class ReviewBootstrapJobError(RuntimeError):
    """bootstrap 作业提交/恢复阶段的可预期错误（由 API 层转 4xx）。"""


def _parse_metadata(job_run: SchedulerJobRun) -> dict[str, Any]:
    """解析 SchedulerJobRun.metadata_json（损坏时降级为空 dict）。"""
    if not job_run.metadata_json:
        return {}
    try:
        parsed = json.loads(job_run.metadata_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "[ReviewBootstrapJob] metadata_json 解析失败，降级为空: job_run_id=%s",
            job_run.id,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_run_key(*, input_hash: str, dry_run: bool) -> str:
    """构造幂等键。

    dry_run 与 apply 使用不同 run_key：同一输入范围的 dry-run 不应该
    阻塞随后的 apply（两者是"先核对再执行"的正常顺序）。
    """
    mode = "dryrun" if dry_run else "apply"
    return f"{REVIEW_BOOTSTRAP_JOB_NAME}:{mode}:{input_hash}"


async def submit_bootstrap_job(
    db: AsyncSession,
    *,
    operator: str,
    reason: str,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
    algorithm_version: str | None = None,
    dry_run: bool = True,
) -> tuple[SchedulerJobRun, bool, dict[str, Any]]:
    """提交 bootstrap 任务（仅创建 queued 记录，不执行计算）。

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        operator: 执行人标识（必填，审计用）
        reason: 执行原因（必填，审计用）
        end_date: 截止交易日（None=最近一个完整 A 股交易日）
        days_back: 回溯交易日数（默认 120，最低 60）
        algorithm_version: 显式算法版本（None=当前 REVIEW_ALGORITHM_VERSION）
        dry_run: True=只计算不写业务数据（默认）

    Returns:
        ``(job_run, is_new, resolved)``；``resolved`` 含解析后的
        end_date / days_back / algorithm_version / input_hash / run_key。

    Raises:
        ReviewBootstrapJobError: 参数非法，或抢锁失败（并发重复提交）。
    """
    if not operator or not operator.strip():
        raise ReviewBootstrapJobError("operator 必填（审计要求）")
    if not reason or not reason.strip():
        raise ReviewBootstrapJobError("reason 必填（审计要求）")
    if days_back < MIN_BOOTSTRAP_DAYS:
        raise ReviewBootstrapJobError(
            f"days_back 最低 {MIN_BOOTSTRAP_DAYS}，当前 {days_back}",
        )

    resolved_algorithm_version = algorithm_version or BOOTSTRAP_ALGORITHM_VERSION
    if resolved_algorithm_version != BOOTSTRAP_ALGORITHM_VERSION:
        raise ReviewBootstrapJobError(
            f"algorithm_version 不匹配当前 Review 算法版本: "
            f"传入 {resolved_algorithm_version}，当前 {BOOTSTRAP_ALGORITHM_VERSION}",
        )

    # end_date 为空必须解析为最近完整交易日（不得直接用自然日 today）
    resolved_end_date = await resolve_bootstrap_end_date(db, end_date=end_date)
    input_hash = compute_input_hash(
        end_date=resolved_end_date,
        days_back=days_back,
        algorithm_version=resolved_algorithm_version,
    )
    run_key = build_run_key(input_hash=input_hash, dry_run=dry_run)

    resolved = {
        "end_date": resolved_end_date.isoformat(),
        "days_back": days_back,
        "algorithm_version": resolved_algorithm_version,
        "input_hash": input_hash,
        "run_key": run_key,
        "dry_run": dry_run,
    }

    job_run, is_new = await acquire_job_run_lock(
        db,
        run_key=run_key,
        job_name=REVIEW_BOOTSTRAP_JOB_NAME,
        business_date=resolved_end_date.isoformat(),
        lease_seconds=REVIEW_BOOTSTRAP_LEASE_SECONDS,
        metadata={
            **resolved,
            "operator": operator.strip(),
            "reason": reason.strip(),
            "bootstrap_status": "queued",
            "submitted_at": datetime.now(UTC).isoformat(),
        },
        # 关键：queued 而非 running —— API 不执行，由 Worker 领取
        initial_status="queued",
    )
    if job_run is None:
        raise ReviewBootstrapJobError(
            f"并发提交冲突，未能获取任务锁: run_key={run_key}",
        )

    logger.info(
        "[ReviewBootstrapJob] 提交: job_run_id=%s is_new=%s dry_run=%s "
        "end_date=%s days_back=%d operator=%s input_hash=%s",
        job_run.id, is_new, dry_run, resolved_end_date.isoformat(),
        days_back, operator, input_hash,
    )
    return job_run, is_new, resolved


async def get_bootstrap_job(
    db: AsyncSession,
    job_run_id: uuid.UUID,
) -> SchedulerJobRun:
    """加载 bootstrap 任务并校验 job_name。

    Raises:
        ReviewBootstrapJobError: 任务不存在或不是 bootstrap 任务。
    """
    job_run = await db.get(SchedulerJobRun, job_run_id)
    if job_run is None:
        raise ReviewBootstrapJobError(f"bootstrap 任务不存在: job_run_id={job_run_id}")
    if job_run.job_name != REVIEW_BOOTSTRAP_JOB_NAME:
        raise ReviewBootstrapJobError(
            f"任务非 review bootstrap: job_name={job_run.job_name}",
        )
    return job_run


def _flatten_scope_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐日 results 摊平成 (trade_date, scope_type, scope_key) 明细行。

    整日 bootstrap_unavailable 时，该日的每个 scope 都记为 unavailable
    并继承整日 reason，使明细与 scope_counts 口径一致。
    """
    rows: list[dict[str, Any]] = []
    for day in results:
        trade_date = day.get("trade_date")
        day_status = day.get("status")
        day_reason = day.get("reason")
        for scope in day.get("scopes") or []:
            unavailable_day = day_status == "bootstrap_unavailable"
            rows.append({
                "trade_date": trade_date,
                "scope_type": scope.get("scope_type"),
                "scope_key": scope.get("scope_key"),
                "status": (
                    "bootstrap_unavailable" if unavailable_day else scope.get("status")
                ),
                "reason": (
                    scope.get("reason") or day_reason if unavailable_day
                    else scope.get("reason")
                ),
                "eligible_count": scope.get("eligible_count"),
                "ready_count": scope.get("ready_count"),
                "coverage": scope.get("coverage"),
            })
    return rows


def build_status_payload(
    job_run: SchedulerJobRun,
    *,
    offset: int = 0,
    limit: int = DEFAULT_DETAIL_PAGE_SIZE,
) -> dict[str, Any]:
    """构造 status 响应：全局 summary + 分页 scope 明细。

    明细分页而非全量返回：120 日 × 数百 scope 会产生上万行，
    单次返回既拖慢响应也超出实际排查需要。
    """
    limit = max(1, min(limit, MAX_DETAIL_PAGE_SIZE))
    offset = max(0, offset)

    meta = _parse_metadata(job_run)
    summary = meta.get("bootstrap_summary") or {}
    rows = _flatten_scope_results(meta.get("bootstrap_results") or [])

    return {
        "job_run_id": str(job_run.id),
        "job_name": job_run.job_name,
        "status": job_run.status,
        "bootstrap_status": meta.get("bootstrap_status", job_run.status),
        "dry_run": bool(meta.get("dry_run", True)),
        "operator": meta.get("operator"),
        "reason": meta.get("reason"),
        "input_hash": meta.get("input_hash"),
        "end_date": meta.get("end_date"),
        "days_back": meta.get("days_back"),
        "algorithm_version": meta.get("algorithm_version"),
        "summary": {
            "eligible_dates": summary.get("eligible_dates", 0),
            "processed": summary.get("processed", 0),
            "skipped": summary.get("skipped", 0),
            "written": summary.get("written", 0),
            "scope_counts": summary.get(
                "scope_counts", dict.fromkeys(SCOPE_COUNT_KEYS, 0),
            ),
            "reason_codes": summary.get("reason_codes", {}),
        },
        "scope_results": rows[offset:offset + limit],
        "scope_results_total": len(rows),
        "offset": offset,
        "limit": limit,
        "started_at": job_run.started_at.isoformat() if job_run.started_at else None,
        "finished_at": job_run.finished_at.isoformat() if job_run.finished_at else None,
        "heartbeat_at": (
            job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None
        ),
        "error_code": job_run.error_code,
        "error_message": job_run.error_message,
    }


async def resume_bootstrap_job(
    db: AsyncSession,
    job_run_id: uuid.UUID,
) -> SchedulerJobRun:
    """把 failed / interrupted 的 bootstrap 任务重置为 queued，由 Worker 重新领取。

    幂等：已 queued 时直接返回同一任务。
    bootstrap 本身按交易日幂等（已写入日期会复用既有 run），
    因此重跑不会重复写入，只补齐未完成部分。

    Raises:
        ReviewBootstrapJobError: 任务不存在 / 非 bootstrap / 状态不可恢复。
    """
    result = await db.execute(
        select(SchedulerJobRun)
        .where(SchedulerJobRun.id == job_run_id)
        .with_for_update(),
    )
    job_run = result.scalar_one_or_none()
    if job_run is None:
        raise ReviewBootstrapJobError(f"bootstrap 任务不存在: job_run_id={job_run_id}")
    if job_run.job_name != REVIEW_BOOTSTRAP_JOB_NAME:
        raise ReviewBootstrapJobError(
            f"任务非 review bootstrap: job_name={job_run.job_name}",
        )

    if job_run.status == "queued":
        return job_run
    if job_run.status not in _RESUMABLE_STATUSES:
        raise ReviewBootstrapJobError(
            f"仅 failed/interrupted 状态可恢复: current_status={job_run.status}",
        )

    meta = _parse_metadata(job_run)
    meta["bootstrap_status"] = "queued"
    meta["resume_requested_at"] = datetime.now(UTC).isoformat()

    job_run.status = "queued"
    job_run.error_code = None
    job_run.error_message = None
    job_run.finished_at = None
    job_run.worker_instance_id = None
    job_run.heartbeat_at = None
    job_run.lease_expires_at = None
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db.flush()

    logger.info("[ReviewBootstrapJob] 恢复为 queued: job_run_id=%s", job_run.id)
    return job_run


async def execute_bootstrap_job(
    db: AsyncSession,
    *,
    job_metadata: dict[str, Any],
) -> dict[str, Any]:
    """执行一次 bootstrap（供 Worker 调用）。

    dry_run 时**不 commit**（严格零业务写入，只回滚计算产生的任何中间状态）；
    apply 时由本函数 commit 业务数据，任务终态由 Worker 的 fenced
    ``finalize_job_run`` 单独写入。

    Args:
        db: 异步会话（本函数控制业务数据的 commit/rollback）
        job_metadata: SchedulerJobRun.metadata_json 解析结果

    Returns:
        ``bootstrap_history`` 的完整返回值。
    """
    dry_run = bool(job_metadata.get("dry_run", True))
    days_back = int(job_metadata.get("days_back", DEFAULT_BOOTSTRAP_DAYS))
    end_date_str = job_metadata.get("end_date")
    end_date = date.fromisoformat(end_date_str) if end_date_str else None

    # [FIX-20260802] Worker 路径同样受内存分片与 RSS 预算约束。
    #   缺省沿用 service 常量；job metadata 可覆盖以便对超大窗口分批续跑。
    chunk_days = int(job_metadata.get("chunk_days", DEFAULT_BOOTSTRAP_CHUNK_DAYS))
    memory_budget_mb = int(
        job_metadata.get("memory_budget_mb", DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB),
    )

    result = await bootstrap_history(
        db,
        end_date=end_date,
        days_back=days_back,
        dry_run=dry_run,
        algorithm_version=job_metadata.get("algorithm_version"),
        operator=job_metadata.get("operator"),
        reason=job_metadata.get("reason"),
        chunk_days=chunk_days,
        memory_budget_mb=memory_budget_mb,
    )

    if dry_run:
        # 严格零业务写入：dry-run 路径不应产生任何待提交变更，显式回滚兜底
        await db.rollback()
    else:
        await db.commit()
    return result


def build_job_metadata_updates(result: dict[str, Any]) -> dict[str, Any]:
    """把 bootstrap 结果压缩为写回 SchedulerJobRun metadata 的字段。

    summary 常驻（体积小、查询高频）；results 全量保留供 status 分页读取。
    """
    return {
        "bootstrap_status": result.get("status", "unknown"),
        "bootstrap_summary": {
            "eligible_dates": result.get("eligible_dates", 0),
            "processed": result.get("processed", 0),
            "skipped": result.get("skipped", 0),
            "written": result.get("written", 0),
            "scope_counts": result.get(
                "scope_counts", dict.fromkeys(SCOPE_COUNT_KEYS, 0),
            ),
            "reason_codes": result.get("reason_codes", {}),
            # [FIX-20260802] 内存可观测性：便于判断任务是否触顶预算而提前停止
            "peak_rss_mb": result.get("peak_rss_mb"),
            "chunks": result.get("chunks"),
            # [CHANGE-20260804 / DS-107] 透传长任务资源治理状态：
            #   stop_reason 安全停止原因；resume_token 断点续跑凭证；progress 进度。
            "stop_reason": result.get("stop_reason"),
            "resume_token": result.get("resume_token"),
            "progress": result.get("progress"),
        },
        "bootstrap_results": result.get("results", []),
        "completed_at": datetime.now(UTC).isoformat(),
    }


if __name__ == "__main__":
    print(f"REVIEW_BOOTSTRAP_JOB_NAME = {REVIEW_BOOTSTRAP_JOB_NAME}")
    print(f"REVIEW_BOOTSTRAP_LEASE_SECONDS = {REVIEW_BOOTSTRAP_LEASE_SECONDS}")

    # run_key: dry-run 与 apply 必须不同，否则 dry-run 会阻塞随后的 apply
    dry_key = build_run_key(input_hash="abc123", dry_run=True)
    apply_key = build_run_key(input_hash="abc123", dry_run=False)
    assert dry_key != apply_key, "dry-run 与 apply 的 run_key 必须不同"
    assert "dryrun" in dry_key and "apply" in apply_key
    print(f"run_key(dry)   = {dry_key}")
    print(f"run_key(apply) = {apply_key}")

    # 摊平：整日不可用时每个 scope 都记 unavailable 并继承整日 reason
    flat = _flatten_scope_results([
        {
            "trade_date": "2026-07-30",
            "status": "completed",
            "scopes": [
                {"scope_type": "market", "scope_key": "market", "status": "insufficient_history"},
                {"scope_type": "concept", "scope_key": "c1", "status": "bootstrap_unavailable", "reason": "pit_membership_empty"},
            ],
        },
        {
            "trade_date": "2026-07-29",
            "status": "bootstrap_unavailable",
            "reason": "source_run_identity_missing",
            "scopes": [
                {"scope_type": "market", "scope_key": "market", "status": "insufficient_history"},
            ],
        },
    ])
    assert len(flat) == 3, f"摊平行数应为 3，实际 {len(flat)}"
    assert flat[2]["status"] == "bootstrap_unavailable", "整日不可用应覆盖 scope 状态"
    assert flat[2]["reason"] == "source_run_identity_missing", "应继承整日 reason"
    print(f"摊平明细行数 = {len(flat)}")

    # metadata 压缩
    updates = build_job_metadata_updates({
        "status": "ok",
        "eligible_dates": 2,
        "processed": 2,
        "written": 2,
        "scope_counts": {"succeeded": 1, "skipped": 0, "unavailable": 2, "failed": 0},
        "reason_codes": {"pit_membership_empty": 1},
        "results": [],
    })
    assert updates["bootstrap_summary"]["written"] == 2
    assert set(updates["bootstrap_summary"]["scope_counts"]) == set(SCOPE_COUNT_KEYS)
    print("OK: review_bootstrap_job_service 自测通过")
