"""生产侧 Board/Concept 快照 importer（[BOARD-LOCAL-OWNERSHIP-01]）。

用法（由本地 `scripts/ops/panji-board-sync` 通过 SSH stdin 调用）：

    python -m app.cli.board_snapshot_import < envelope.json

职责与边界：
- 从 **stdin** 读取版本化 envelope（schema_version=1），校验后重建 BoardSnapshot。
- 通过**唯一** DB 业务写 owner `board_sync_service.sync_boards()` 原子写库
  （本模块**不含**任何手写 INSERT/UPDATE/DELETE 业务变更）。
- `effective_date` 由**服务端**决定（上海日历当日），不信任客户端传入。
- single-flight：用 PostgreSQL 事务级 advisory lock 防止两个手动同步并发写；
  这是**并发保护**，**不是频率限制** —— 同日可重复同步、相同快照可安全重放。
- 失败：整体 rollback（保留上一成功快照），退出码非零。
- Market Dashboard race safety：沿用既有 SHARE 表锁 guard
  （market_dashboard_projection_rebuild_service 的 membership commit guard），
  本模块不做任何绕过/改写；不自动触发 dashboard rebuild。

安全：stdin 中**绝不**出现 cookie；envelope 由 board_snapshot_transfer 保证。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.services.board_snapshot_transfer import (
    MAX_ENVELOPE_BYTES,
    BoardSnapshotTransferError,
    deserialize_envelope,
    reconstruct_snapshot,
    validate_envelope,
)
from app.services.board_sync_service import (
    record_sync_status,
    resolve_board_instruments,
    sync_boards,
)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

#: single-flight advisory lock key（事务级；自动随事务结束释放）。
#: 固定常量，仅用于串行化「手动板块同步」这一件事，不是频率/冷却限制。
_BOARD_SYNC_ADVISORY_LOCK_KEY = 0x626F61726473796E  # b"boardsyn"


class BoardSyncBusyError(RuntimeError):
    """已有另一个手动板块同步在进行（single-flight 并发保护）。"""


def _read_stdin_envelope() -> bytes:
    """从 stdin 读取 envelope 原文，带最大字节数保护。"""
    raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise BoardSnapshotTransferError(
            f"stdin envelope 超过上限 {MAX_ENVELOPE_BYTES} bytes"
        )
    return raw


def _server_effective_date():
    """服务端决定 effective_date：上海日历当日。"""
    return datetime.now(_SHANGHAI_TZ).date()


async def _apply(envelope_bytes: bytes) -> dict[str, int | str | None]:
    """校验 envelope 并通过 sync_boards 原子应用（同一事务 + single-flight）。"""
    envelope = deserialize_envelope(envelope_bytes)
    validate_envelope(envelope)
    snapshot = reconstruct_snapshot(envelope)

    effective_date = _server_effective_date()
    start = time.monotonic()

    async with AsyncSessionLocal() as db:
        async with db.begin():
            # single-flight（事务级 advisory lock，随事务自动释放）
            acquired = await db.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    f"{_BOARD_SYNC_ADVISORY_LOCK_KEY})"
                )
            )
            if not acquired:
                raise BoardSyncBusyError(
                    "another board sync is already running"
                )

            # 唯一业务写 owner；同一 session/transaction 内解析 instrument
            result = await sync_boards(
                db,
                snapshot,
                instrument_resolver=lambda symbols: resolve_board_instruments(
                    db, symbols
                ),
                effective_date=effective_date,
            )

    duration_ms = int((time.monotonic() - start) * 1000)
    return {
        "status": "succeeded",
        "source": "wencai",
        "mode": "local_manual",
        "raw_rows": result.get("raw_rows"),
        "resolved": result.get("resolved"),
        "unresolved": result.get("unresolved"),
        "industry_count": result.get("industry_count"),
        "concept_count": result.get("concept_count"),
        "membership_count": result.get("membership_count"),
        "duration_ms": duration_ms,
        "effective_date": effective_date.isoformat(),
        "error_code": None,
        "reused_previous_snapshot": False,
    }


async def _record_status_best_effort(status: dict) -> None:
    """写 Redis 最近状态：失败**不得**把已成功的 DB 事务升级为失败。"""
    try:
        await record_sync_status(status)
    except Exception as exc:  # noqa: BLE001 - Redis 诊断失败不影响 DB 结果
        print(f"warning: record_sync_status failed: {exc}", file=sys.stderr)


def _print_summary(summary: dict) -> None:
    for key in (
        "status",
        "mode",
        "source",
        "effective_date",
        "raw_rows",
        "industry_count",
        "concept_count",
        "membership_count",
        "resolved",
        "unresolved",
        "duration_ms",
        "error_code",
    ):
        print(f"{key}={summary.get(key)}")


async def _amain() -> int:
    try:
        envelope_bytes = _read_stdin_envelope()
    except BoardSnapshotTransferError as exc:
        print(f"status=failed\nerror_code=STDIN_READ_FAILED\nerror={exc}")
        return 1

    try:
        summary = await _apply(envelope_bytes)
    except BoardSyncBusyError as exc:
        await _record_status_best_effort({
            "status": "failed",
            "source": "wencai",
            "mode": "local_manual",
            "error_code": "BOARD_SYNC_BUSY",
            "reused_previous_snapshot": True,
        })
        print(f"status=failed\nerror_code=BOARD_SYNC_BUSY\nerror={exc}")
        return 1
    except BoardSnapshotTransferError as exc:
        await _record_status_best_effort({
            "status": "failed",
            "source": "wencai",
            "mode": "local_manual",
            "error_code": type(exc).__name__,
            "reused_previous_snapshot": True,
        })
        print(f"status=failed\nerror_code={type(exc).__name__}\nerror={exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - 任何失败都 rollback 且非零退出
        await _record_status_best_effort({
            "status": "failed",
            "source": "wencai",
            "mode": "local_manual",
            "error_code": type(exc).__name__,
            "reused_previous_snapshot": True,
        })
        print(f"status=failed\nerror_code={type(exc).__name__}\nerror={exc}")
        return 1

    await _record_status_best_effort(summary)
    _print_summary(summary)
    return 0


def main() -> int:
    """CLI 入口：返回进程退出码（0=成功，非零=失败）。"""
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
