"""DSA Recovery CLI - 恢复失败的 DSA run（P0-2 正式入口）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.dsa_recovery_cli \\
        --job-run-id <uuid>

    # dry-run：只检查状态，不创建新 run
    .venv/bin/python -m scripts.dsa_recovery_cli --job-run-id <uuid> --dry-run

参数：
    --job-run-id: orchestrator SchedulerJobRun.id（必填）
    --dry-run: 只检查 DSA run 状态，不创建新 run

退出码：
    0: 恢复成功（或 dry-run 状态可恢复）
    1: 恢复失败（DSA 正在执行 / 恢复次数超限 / 状态不支持）
    2: 参数错误 / job_run 不存在

约束：
    - 禁止裸 SQL、/tmp Python、docker cp
    - 原失败 run 保留审计，只创建新 run
    - 管理员应急能力，需人工确认后执行
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid

logger = logging.getLogger("dsa_recovery_cli")


async def _run(args: argparse.Namespace) -> int:
    from app.db import AsyncSessionLocal
    from app.services.dsa_recovery_service import (
        DSARecoveryError,
        get_dsa_recovery_status,
        recover_failed_dsa_run,
    )

    try:
        job_run_id = uuid.UUID(args.job_run_id)
    except ValueError:
        print(f"错误: --job-run-id 不是合法 UUID: {args.job_run_id}", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as db:
        # 先检查状态
        status = await get_dsa_recovery_status(db, job_run_id=job_run_id)
        print(json.dumps(status, indent=2, default=str))

        if args.dry_run:
            print("[dry-run] 未创建新 run", file=sys.stderr)
            return 0

        if not status.get("can_recover", False):
            print(
                f"错误: 当前状态不可恢复: {status.get('reason', 'unknown')}",
                file=sys.stderr,
            )
            return 1

        try:
            new_run, is_new = await recover_failed_dsa_run(
                db, job_run_id=job_run_id,
            )
            await db.commit()
            print(
                f"恢复成功: new_run_id={new_run.id}, attempt_no={new_run.attempt_no}, "
                f"is_new={is_new}",
            )
            return 0
        except DSARecoveryError as exc:
            print(f"错误: DSA 恢复失败: {exc}", file=sys.stderr)
            return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DSA Recovery CLI - 恢复失败的 DSA run",
    )
    parser.add_argument(
        "--job-run-id", required=True,
        help="orchestrator SchedulerJobRun.id (UUID)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只检查状态，不创建新 run",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
