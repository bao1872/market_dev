"""[ChipConsensusWorker] chip consensus 轮询 owner（W7B1：从 worker.py 迁移，仅搬 owner 不改状态机）。

单一 public 入口 poll_chip_consensus_once：
- Chip 为独立 standalone debug Worker（WORKER_TYPE=chip_consensus）的领取 + 执行 owner；
- 所有状态阶段（claim / lease / heartbeat / domain-run init / compute / domain finalize /
  publication / auction upgrade / SchedulerJobRun terminal / cleanup）逐字保留；
- 不重构内部阶段、不改异常边界、不改 publication / finalize / heartbeat 顺序。

依赖注入（机械替换，执行体等价于迁移前）：
- AsyncSessionLocal       → session_factory
- _WORKER_INSTANCE_ID     → worker_instance_id
- logger                   → 注入 logger
其余 worker 全局（CHIP_CONSENSUS_JOB_NAME / _CHIP_LEASE_SECONDS /
CHIP_CONSENSUS_ALGORITHM_VERSION）为函数内 lazy import，不随文件迁移改变。
"""
from __future__ import annotations

import uuid


async def poll_chip_consensus_once(
    *,
    session_factory,
    worker_instance_id: str,
    logger,
) -> bool:
    """[ChipConsensusWorker] - 单次轮询：领取并执行一个 queued/resume_queued chip consensus 任务。

    使用 SELECT ... FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    领取后更新 status='running' + worker_instance_id + heartbeat + lease + lease_epoch（fencing）。
    然后调用 execute_after_close_chip_consensus（含断点续算）。

    [P0-3 ref/instruction.md §二.3] chip 任务有执行者：
    - 在现有 after-close worker 容器内增加独立 poll 函数和 WORKER_TYPE 分支
    - 不新增常驻容器
    - 使用 FOR UPDATE SKIP LOCKED、lease_epoch、heartbeat、断点续算
    - chip 失败不反改 core（execute_after_close_chip_consensus 内部已隔离）

    断点续算：
    - get_pending_chip_instruments 过滤已 succeeded 的 instrument
    - resume_queued 任务只重试未成功项
    - 部分成功写 metadata.chip_status=partial，主 status=succeeded

    Returns:
        True 如果领取到任务（无论执行成功与否），False 如果无 queued/resume_queued 任务
    """
    from datetime import date as date_cls

    from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION
    from app.services.after_close_chip_consensus_service import (
        _CHIP_LEASE_SECONDS,
        CHIP_CONSENSUS_JOB_NAME,
        execute_after_close_chip_consensus,
        get_pending_chip_instruments,
    )
    from app.services.chip_consensus_run_lifecycle import (
        META_CHIP_RUN_ID,
        finalize_chip_run,
        resolve_or_create_chip_run,
    )
    from app.services.feature_snapshot_service import (
        get_active_a_share_instruments,
    )
    from app.services.fenced_job_run_service import (
        FencedJobHeartbeat,
        JobLeaseLostError,
        claim_next_job_run,
        finalize_job_run,
        merge_owned_job_run_metadata,
    )

    async with session_factory() as db:
        claim = await claim_next_job_run(
            db,
            job_name=CHIP_CONSENSUS_JOB_NAME,
            worker_instance_id=worker_instance_id,
            lease_seconds=_CHIP_LEASE_SECONDS,
        )
        if claim is None:
            await db.rollback()
            return False
        await db.commit()

    lease_token = claim.token
    meta = claim.metadata
    trade_date_str = meta.get("trade_date")
    core_run_id_str = meta.get("core_run_id")
    job_run_id = lease_token.job_run_id
    current_lease_epoch = lease_token.lease_epoch
    prev_status = claim.previous_status
    is_resume = prev_status == "resume_queued"

    logger.info(
        "[ChipConsensusWorker] 领取任务: job_run_id=%s, prev_status=%s, "
        "lease_epoch=%s, is_resume=%s",
        job_run_id, prev_status, current_lease_epoch, is_resume,
    )

    heartbeat = FencedJobHeartbeat(lease_token, interval_seconds=30.0)
    await heartbeat.start()
    finalized = False
    trade_date = None
    chip_status = "failed"
    # [Corrective-3 §二.1] 领域 run id：retry/resume 必须复用 metadata 中已固定的 id，
    # 禁止每次重试新建 ChipConsensusRun。
    chip_run_id: uuid.UUID | None = None
    existing_chip_run_id_str = meta.get(META_CHIP_RUN_ID)
    existing_chip_run_id: uuid.UUID | None = None
    if existing_chip_run_id_str:
        try:
            existing_chip_run_id = uuid.UUID(str(existing_chip_run_id_str))
        except (ValueError, TypeError):
            logger.warning(
                "[ChipConsensusWorker] metadata.chip_run_id 非法，忽略: %s",
                existing_chip_run_id_str,
            )

    async def _finalize_failure(code: str, message: str) -> bool:
        return await finalize_job_run(
            lease_token,
            status="failed",
            metadata_updates={
                "chip_status": "failed",
                "chip_results_summary": {
                    "succeeded": 0,
                    "failed": 1,
                    "skipped": 0,
                    "total": 1,
                    "reason_codes": [code],
                },
            },
            total_count=1,
            succeeded_count=0,
            failed_count=1,
            error_code=code,
            error_message=message[:500],
        )

    try:
        if not trade_date_str or not core_run_id_str:
            logger.error(
                "[ChipConsensusWorker] 任务缺少 trade_date/core_run_id: job_run_id=%s",
                job_run_id,
            )
            finalized = await _finalize_failure(
                "CHIP_JOB_METADATA_MISSING",
                "任务缺少 trade_date/core_run_id，无法执行 chip consensus",
            )
            return True

        trade_date = date_cls.fromisoformat(trade_date_str)
        core_run_id = uuid.UUID(core_run_id_str)

        try:
            heartbeat.ensure_owned()
            async with session_factory() as db:
                all_instrument_ids = await get_active_a_share_instruments(db)
                pending_instrument_ids = await get_pending_chip_instruments(
                    db,
                    trade_date=trade_date,
                    core_run_id=core_run_id,
                    all_instrument_ids=all_instrument_ids,
                )
            heartbeat.ensure_owned()
        except JobLeaseLostError:
            raise
        except Exception as exc:
            logger.exception(
                "[ChipConsensusWorker] 获取 instrument 列表失败: job_run_id=%s, error=%s",
                job_run_id, exc,
            )
            finalized = await _finalize_failure(
                "CHIP_INSTRUMENT_LIST_FAILED",
                f"获取 instrument 列表失败: {exc}",
            )
            return True

        # [Corrective-3 §二.1] 建立 ChipConsensusRun 生命周期。
        # 修复前：没有任何生产路径写入 chip_consensus_runs，导致
        # publish_chip_consensus 的 session.get(ChipConsensusRun, ...) 永远为空。
        try:
            heartbeat.ensure_owned()
            async with session_factory() as run_db:
                chip_run = await resolve_or_create_chip_run(
                    run_db,
                    trade_date=trade_date,
                    source_core_run_id=core_run_id,
                    algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
                    scheduler_job_run_id=job_run_id,
                    expected_count=len(all_instrument_ids),
                    worker_id=worker_instance_id,
                    lease_epoch=current_lease_epoch,
                    existing_run_id=existing_chip_run_id,
                )
                chip_run_id = chip_run.id
                await run_db.commit()
            # 把 chip_run_id 固定到 SchedulerJobRun metadata，恢复任务时复用同一 ID
            if existing_chip_run_id != chip_run_id:
                await merge_owned_job_run_metadata(
                    lease_token, {META_CHIP_RUN_ID: str(chip_run_id)},
                )
            heartbeat.ensure_owned()
        except JobLeaseLostError:
            raise
        except Exception as exc:
            logger.exception(
                "[ChipConsensusWorker] 创建/解析 ChipConsensusRun 失败: "
                "job_run_id=%s, error=%s",
                job_run_id, exc,
            )
            finalized = await _finalize_failure(
                "CHIP_DOMAIN_RUN_INIT_FAILED",
                f"创建/解析 ChipConsensusRun 失败: {exc}",
            )
            return True

        logger.info(
            "[ChipConsensusWorker] 开始执行: job_run_id=%s, trade_date=%s, "
            "core_run_id=%s, chip_run_id=%s, total_instruments=%d, pending=%d, "
            "is_resume=%s",
            job_run_id, trade_date, core_run_id, chip_run_id,
            len(all_instrument_ids), len(pending_instrument_ids), is_resume,
        )

        chip_result_summary = await execute_after_close_chip_consensus(
            job_run_id=job_run_id,
            trade_date=trade_date,
            core_run_id=core_run_id,
            instrument_ids=pending_instrument_ids,
            worker_id=worker_instance_id,
            lease_epoch=current_lease_epoch,
            ownership_check=heartbeat.ensure_owned,
        )
        heartbeat.ensure_owned()
        chip_status = str(chip_result_summary.get("status", "failed"))
        main_status = "failed" if chip_status == "failed" else "succeeded"
        failed_items = chip_result_summary.get("failed_instruments", [])
        skipped_items = chip_result_summary.get("skipped_instruments", [])
        reason_codes = sorted({
            str(item.get("reason") or item.get("error") or "UNKNOWN")[:120]
            for item in [*failed_items, *skipped_items]
        })[:20]
        metadata_updates = {
            "chip_status": chip_status,
            "succeeded_count": chip_result_summary.get("succeeded_count", 0),
            "failed_count": chip_result_summary.get("failed_count", 0),
            "skipped_count": chip_result_summary.get("skipped_count", 0),
            "total_count": chip_result_summary.get("total_count", 0),
            "chip_results_summary": {
                "succeeded": chip_result_summary.get("succeeded_count", 0),
                "failed": chip_result_summary.get("failed_count", 0),
                "skipped": chip_result_summary.get("skipped_count", 0),
                "total": chip_result_summary.get("total_count", 0),
                "reason_codes": reason_codes,
            },
        }

        # [Corrective-3 §二.1/§二.3] chip snapshots 完成 → ChipConsensusRun 终态。
        # 必须先于 publish_chip_consensus，因为发布函数校验
        # chip_run.status ∈ (succeeded, partial) 并读取 coverage_ratio。
        #
        # [Corrective-3.1 §P0-2] 领域 run 终态写入失败不得被静默吞掉：
        # 失败时记录 chip_domain_finalize_* 治理字段、禁止 publication、
        # 并把主任务降级为 degraded（不再无条件 succeeded）。
        domain_finalized = False
        if chip_run_id is not None:
            try:
                async with session_factory() as run_db:
                    await finalize_chip_run(
                        run_db,
                        chip_run_id=chip_run_id,
                        chip_status=chip_status,
                        succeeded_count=int(
                            chip_result_summary.get("succeeded_count", 0),
                        ),
                        failed_count=int(chip_result_summary.get("failed_count", 0)),
                        skipped_count=int(chip_result_summary.get("skipped_count", 0)),
                        total_count=int(chip_result_summary.get("total_count", 0)),
                        error_code=(
                            "CHIP_SYSTEMIC_FAILURE" if chip_status == "failed" else None
                        ),
                        error_message=(
                            "全部 chip instrument 处理失败"
                            if chip_status == "failed" else None
                        ),
                        diagnostics={"reason_codes": reason_codes},
                        fenced_token=heartbeat.token,
                    )
                    await run_db.commit()
                domain_finalized = True
                metadata_updates["chip_domain_finalize_status"] = "succeeded"
            except Exception as exc:
                logger.warning(
                    "[ChipConsensusWorker] ChipConsensusRun 终态写入失败: chip_run_id=%s",
                    chip_run_id, exc_info=True,
                )
                metadata_updates["chip_domain_finalize_status"] = "failed"
                metadata_updates["chip_domain_finalize_error_code"] = (
                    "CHIP_DOMAIN_FINALIZE_FAILED"
                )
                metadata_updates["chip_domain_finalize_error"] = str(exc)[:500]
                metadata_updates["chip_run_id"] = str(chip_run_id)
                # 领域 run 状态未知/不一致 → 主任务不得声称成功。
                # 不引入 SchedulerJobRun 状态机之外的新值（合法值仅
                # queued/running/succeeded/failed/skipped/interrupted/resume_queued），
                # 因此统一落 failed，由 metadata 区分"快照已算完但领域终态失败"。
                main_status = "failed"
        else:
            metadata_updates["chip_domain_finalize_status"] = "skipped_no_run"

        # [Corrective-3.1 §P0-1] publication 必须在 SchedulerJobRun 终态之前、
        # 且在租约仍然持有时执行，并向下传递 ownership_check 做写前 fencing。
        # 修复前 publication 位于 finally: heartbeat.stop() 之后，租约已释放，
        # helper 的 fencing 能力在生产路径上完全没有生效。
        publication_outcome = None
        if (
            trade_date is not None
            and chip_run_id is not None
            and domain_finalized
            and chip_status in {"succeeded", "partial"}
        ):
            from app.services.auction_anchor_service import (
                generate_and_publish_auction_anchors,
            )
            from app.services.chip_consensus_run_lifecycle import (
                publish_chip_and_upgrade_auction,
            )
            from app.services.factor_publication_service import publish_chip_consensus

            heartbeat.ensure_owned()
            publication_outcome = await publish_chip_and_upgrade_auction(
                trade_date=trade_date,
                chip_run_id=chip_run_id,
                algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
                chip_status=chip_status,
                scheduler_job_run_id=job_run_id,
                worker_id=worker_instance_id,
                lease_epoch=current_lease_epoch,
                anchor_rebuild_required=bool(
                    chip_result_summary.get("anchor_rebuild_required", False),
                ),
                session_factory=session_factory,
                publish_fn=publish_chip_consensus,
                auction_fn=generate_and_publish_auction_anchors,
                ownership_check=heartbeat.ensure_owned,
                fenced_token=heartbeat.token,
            )
            # [Corrective-3 §二.4] 软失败必须可治理：并入主任务终态 metadata，
            # 使 ProductReadiness 能显示 chip run succeeded 但 publication missing。
            metadata_updates.update(publication_outcome.to_metadata())
        elif chip_run_id is not None and not domain_finalized:
            logger.error(
                "[ChipConsensusWorker] 领域 run 终态失败，已阻断 chip publication: "
                "chip_run_id=%s",
                chip_run_id,
            )

        # [Corrective-3.1 §P0-2] 区分两种 failed 原因，不得都报 CHIP_SYSTEMIC_FAILURE：
        #  - chip_status == "failed"：全部 instrument 处理失败
        #  - 领域 run 终态写入失败：快照已算完但 ChipConsensusRun 状态不一致
        if main_status != "failed":
            terminal_error_code = None
            terminal_error_message = None
        elif chip_status == "failed":
            terminal_error_code = "CHIP_SYSTEMIC_FAILURE"
            terminal_error_message = "全部 chip instrument 处理失败"
        else:
            terminal_error_code = "CHIP_DOMAIN_FINALIZE_FAILED"
            terminal_error_message = (
                "chip 快照已完成但 ChipConsensusRun 终态写入失败，"
                "publication 已阻断，需人工核对领域 run 状态"
            )

        finalized = await finalize_job_run(
            lease_token,
            status=main_status,
            metadata_updates=metadata_updates,
            total_count=int(chip_result_summary.get("total_count", 0)),
            succeeded_count=int(chip_result_summary.get("succeeded_count", 0)),
            failed_count=int(chip_result_summary.get("failed_count", 0)),
            error_code=terminal_error_code,
            error_message=terminal_error_message,
        )
        if not finalized:
            raise JobLeaseLostError(
                f"chip terminal update fenced: job_run_id={job_run_id}"
            )

        logger.info(
            "[ChipConsensusWorker] 执行完成: job_run_id=%s, status=%s, "
            "succeeded=%d, failed=%d, skipped=%d, total=%d",
            job_run_id, chip_status,
            chip_result_summary.get("succeeded_count", 0),
            chip_result_summary.get("failed_count", 0),
            chip_result_summary.get("skipped_count", 0),
            chip_result_summary.get("total_count", 0),
        )
    except JobLeaseLostError as exc:
        logger.warning(
            "[ChipConsensusWorker] 已失去租约，禁止终态或后续写入: job_run_id=%s, error=%s",
            job_run_id, exc,
        )
        return True
    except Exception as exc:
        logger.exception(
            "[ChipConsensusWorker] 执行异常: job_run_id=%s, error=%s", job_run_id, exc,
        )
        finalized = await _finalize_failure(
            "CHIP_JOB_EXECUTION_FAILED",
            f"chip consensus 执行异常: {exc}",
        )
        return True
    finally:
        await heartbeat.stop()

    # [Corrective-3.1 §P0-1] publication / auction 已上移至租约保护区内执行
    # （SchedulerJobRun 终态之前，并传入 ownership_check）。此处不再有终态后
    # 的无保护写入。
    return True
