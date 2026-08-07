"""复盘模块 CLI - 触发 review run 计算与发布（PRD §11、§12.6）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.review_compute_cli \\
        --trade-date 2026-07-29 --canary

    # 全量计算（market + 全部 industry_l1）
    .venv/bin/python -m scripts.review_compute_cli --all --publish

    # 自动从最新 stock_core + board_analysis pointer 推断 trade_date
    .venv/bin/python -m scripts.review_compute_cli --all --publish

    # 指定股票列表（debug 用）
    .venv/bin/python -m scripts.review_compute_cli --canary \\
        --symbols 000001 000002 --no-publish

    # 仅 dry-run 校验输入
    .venv/bin/python -m scripts.review_compute_cli --canary --dry-run

参数：
    --canary: canary 模式（限定 5 个 industry_l1 范围）
    --all: 全量计算（market + 全部 industry_l1）
    --trade-date: 指定交易日（默认从最新 stock_core pointer 推断）
    --symbols: 限定股票列表（canary/debug 用，None=不限定）
    --publish: 计算后自动发布（coverage 满足门禁时）
    --no-publish: 不发布，只计算
    --dry-run: 只校验输入，不写 run/snapshot
    --baseline-window: 历史基线窗口（默认 120，最低 60）
    --resume: 重启已有 run（参数为 run_id）
    --only-pending: resume 时只处理 pending/可重试 failed（默认 True）

约束：
    - 输入只读 stock_core 和 board_analysis 的 factor_publications pointer
    - 单 scope 失败不阻塞其他 scope
    - 所有写操作幂等（idempotency_key 由 CLI 自动生成）
    - publish 失败时不重试（需调用 publish 接口或再次运行 --publish）

退出码：
    0: 成功
    1: 计算过程有失败 scope（partial / failed）
    2: 输入校验失败（缺 pointer / 参数错误）
    3: publish 门禁失败（计算成功但发布被阻塞）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import date

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="复盘模块 CLI（触发 review run 计算与发布）",
    )

    # 模式（互斥）
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--canary", action="store_true",
                      help="canary 模式（限定 5 个 industry_l1 范围）")
    mode.add_argument("--all", action="store_true",
                      help="全量计算（market + 全部 industry_l1）")
    mode.add_argument("--resume", type=str, default=None,
                      help="重启已有 run（参数为 run_id UUID）")

    # 通用参数
    p.add_argument("--trade-date", type=str, default=None,
                   help="业务交易日 ISO（默认从最新 stock_core pointer 推断）")
    p.add_argument("--symbols", type=str, nargs="*", default=None,
                   help="限定股票列表（canary/debug 用，None=不限定）")
    p.add_argument("--publish", action="store_true", default=True,
                   help="计算后自动发布（默认启用；门禁失败时不阻塞退出码 0）")
    p.add_argument("--no-publish", dest="publish", action="store_false",
                   help="不发布，只计算")
    p.add_argument("--dry-run", action="store_true",
                   help="只校验输入，不写 run/snapshot")
    p.add_argument("--baseline-window", type=int, default=120,
                   help="历史基线窗口（默认 120，最低 60）")
    p.add_argument("--force-publish", action="store_true",
                   help="强制发布（跳过门禁，仅 debug 用）")

    # resume 参数
    p.add_argument("--only-pending", action="store_true", default=True,
                   help="resume 时只处理 pending/可重试 failed（默认 True）")
    p.add_argument("--all-items", dest="only_pending", action="store_false",
                   help="resume 时重新计算所有非 succeeded item")

    return p.parse_args()


async def _resolve_trade_date(db, trade_date_str: str | None) -> date:
    """从参数或最新 stock_core pointer 解析 trade_date。"""
    if trade_date_str:
        return date.fromisoformat(trade_date_str)

    from sqlalchemy import select

    from app.models.factor_publication import FactorPublication

    stmt = (
        select(FactorPublication)
        .where(FactorPublication.publication_kind == "stock_core")
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    pub = result.scalar_one_or_none()
    if pub is None:
        raise RuntimeError(
            "无已发布 stock_core pointer，必须先完成盘后核心计算并发布 stock_core"
        )
    return pub.trade_date


def _gen_idempotency_key(trade_date: date, suffix: str) -> str:
    """生成幂等键（trade_date + suffix）。"""
    import hashlib
    raw = f"review_cli:{trade_date.isoformat()}:{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _run_create_and_compute(args: argparse.Namespace) -> int:
    """创建并执行 review run。"""
    from app.db import AsyncSessionLocal
    from app.services.review_orchestrator_service import (
        MIN_BASELINE_WINDOW,
        REVIEW_ALGORITHM_VERSION,
        ReviewOrchestratorError,
        compute_run,
        create_run,
        publish_run,
    )
    from app.services.review_publication_service import ReviewPublishBlockError

    if args.baseline_window < MIN_BASELINE_WINDOW:
        logger.error(
            "baseline_window=%d 低于最低值 %d",
            args.baseline_window, MIN_BASELINE_WINDOW,
        )
        return 2

    async with AsyncSessionLocal() as db:
        # 解析 trade_date
        try:
            trade_date = await _resolve_trade_date(db, args.trade_date)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        except ValueError as exc:
            logger.error("trade_date 格式错误: %s", exc)
            return 2

        # canary / all 模式
        canary = args.canary

        # 幂等键
        mode_suffix = "canary" if canary else "all"
        if args.symbols:
            mode_suffix += ":symbols:" + ",".join(args.symbols[:5])
        idem_key = _gen_idempotency_key(trade_date, mode_suffix)

        logger.info(
            "[Review CLI] trade_date=%s, mode=%s, canary=%s, symbols=%s, "
            "publish=%s, dry_run=%s",
            trade_date, mode_suffix, canary, args.symbols,
            args.publish, args.dry_run,
        )
        logger.info(
            "[Review CLI] 算法版本: %s, baseline_window=%d, idempotency_key=%s",
            REVIEW_ALGORITHM_VERSION, args.baseline_window, idem_key,
        )

        try:
            # 1. 创建 run
            creation = await create_run(
                db,
                trade_date=trade_date,
                baseline_window=args.baseline_window,
                canary=canary,
                symbols=args.symbols,
                dry_run=args.dry_run,
                idempotency_key=idem_key,
            )
            run = creation.run
            if args.dry_run:
                print("[DRY-RUN] 输入校验通过:")
                print(f"  trade_date: {trade_date}")
                print(f"  source_core_run_id: {run.source_core_run_id}")
                print(f"  source_board_run_id: {run.source_board_run_id}")
                print(f"  algorithm_version: {run.algorithm_version}")
                print(f"  filter_version: {run.filter_version}")
                print(f"  baseline_window: {run.baseline_window}")
                await db.rollback()
                return 0

            await db.commit()
            await db.refresh(run)
            logger.info("[Review CLI] run 创建: run_id=%s status=%s", run.id, run.status)

            # 2. 执行计算
            compute_result = await compute_run(
                db, run, canary=canary, symbols=args.symbols,
            )
            await db.commit()
            await db.refresh(run)

            print("[Compute Result]")
            print(json.dumps(compute_result, ensure_ascii=False, indent=2, default=str))

            # 3. 自动发布（如启用且门禁通过）
            publish_result: dict[str, object] = {"published": False}
            if args.publish:
                try:
                    publication, blockers = await publish_run(
                        db, run, force=args.force_publish,
                    )
                    await db.commit()
                    await db.refresh(run)
                    publish_result = {
                        "published": True,
                        "publication_id": str(publication.id),
                        "blockers": blockers,
                    }
                    logger.info(
                        "[Review CLI] 发布成功: publication_id=%s",
                        publication.id,
                    )
                except ReviewPublishBlockError as exc:
                    await db.rollback()
                    publish_result = {
                        "published": False,
                        "blockers": exc.blockers,
                        "error": str(exc),
                    }
                    logger.warning(
                        "[Review CLI] 发布门禁失败: %s", exc.blockers,
                    )
                    # 计算成功但发布失败 → 退出码 3
                    print("[Publish Result]")
                    print(json.dumps(publish_result, ensure_ascii=False, indent=2))
                    return 3

            print("[Publish Result]")
            print(json.dumps(publish_result, ensure_ascii=False, indent=2))

            # 退出码：有失败 scope → 1，否则 0
            if compute_result.get("failed_scope_count", 0) > 0:
                return 1
            return 0

        except ReviewOrchestratorError as exc:
            await db.rollback()
            logger.error("编排失败: %s", exc)
            return 2
        except Exception:
            await db.rollback()
            logger.exception("[Review CLI] 失败")
            return 1


async def _run_resume(args: argparse.Namespace) -> int:
    """重启已有 review run。"""
    from app.db import AsyncSessionLocal
    from app.services.review_orchestrator_service import (
        ReviewOrchestratorError,
        get_run,
        publish_run,
        resume_run,
    )
    from app.services.review_publication_service import ReviewPublishBlockError

    try:
        run_id = uuid.UUID(args.resume)
    except ValueError:
        logger.error("run_id 非法 UUID: %s", args.resume)
        return 2

    async with AsyncSessionLocal() as db:
        run = await get_run(db, run_id)
        if run is None:
            logger.error("run 不存在: run_id=%s", run_id)
            return 2

        logger.info(
            "[Review CLI] resume run_id=%s current_status=%s only_pending=%s",
            run_id, run.status, args.only_pending,
        )

        try:
            result = await resume_run(db, run, only_pending=args.only_pending)
            await db.commit()
            await db.refresh(run)

            print("[Resume Result]")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

            # 自动发布（如启用）
            if args.publish:
                try:
                    publication, blockers = await publish_run(
                        db, run, force=args.force_publish,
                    )
                    await db.commit()
                    await db.refresh(run)
                    print(json.dumps({
                        "published": True,
                        "publication_id": str(publication.id),
                        "blockers": blockers,
                    }, ensure_ascii=False, indent=2))
                except ReviewPublishBlockError as exc:
                    await db.rollback()
                    print(json.dumps({
                        "published": False,
                        "blockers": exc.blockers,
                    }, ensure_ascii=False, indent=2))
                    return 3

            if result.get("failed", 0) > 0:
                return 1
            return 0

        except ReviewOrchestratorError as exc:
            await db.rollback()
            logger.error("resume 失败: %s", exc)
            return 2
        except Exception:
            await db.rollback()
            logger.exception("[Review CLI] resume 失败")
            return 1


async def _run(args: argparse.Namespace) -> int:
    if args.resume:
        return await _run_resume(args)
    return await _run_create_and_compute(args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
