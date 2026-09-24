"""本地 Mac 侧 Board/Concept 快照导出器（[BOARD-LOCAL-OWNERSHIP-01]）。

用法（由 `scripts/ops/panji-board-sync` 调用）：

    python -m app.cli.board_snapshot_export --out /tmp/envelope.json \
        [--producer-git-sha <sha>] [--dry-run]

职责：
- 在**本地**调用 wencai_board_provider.fetch_board_snapshot()（本地 cookie 访问问财）；
- 构造版本化 envelope（board_snapshot_transfer.build_envelope）并自校验；
- 把 envelope 写入 `--out`（供 SSH stdin 传输），打印**无敏感信息**的本地摘要。

安全：
- 本地 cookie 只用于本地抓取；envelope 中绝不含 cookie（transfer 模块 fail-closed 保证）。
- 不打印 cookie / 不打印原始 HTTP 响应。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.services.board_snapshot_transfer import (
    build_envelope,
    serialize_envelope,
    validate_envelope,
)
from app.services.wencai_board_provider import fetch_board_snapshot


def _summarize(snapshot, envelope, envelope_bytes: int) -> str:
    industry = sum(1 for b in snapshot.boards if b.get("type") == "industry")
    concept = sum(1 for b in snapshot.boards if b.get("type") == "concept")
    candidate_symbols = {
        s for symbols in snapshot.memberships.values() for s in symbols
    }
    lines = [
        "local_fetch:",
        f"  source={envelope['source']}",
        f"  raw_rows={snapshot.raw_rows}",
        f"  board_count={len(snapshot.boards)}",
        f"  industry_count={industry}",
        f"  concept_count={concept}",
        f"  membership_count={snapshot.membership_count}",
        f"  candidate_symbol_count={len(candidate_symbols)}",
        f"  unresolved_symbol_count={len(snapshot.unresolved_symbols)}",
        f"  envelope_bytes={envelope_bytes}",
        f"  payload_sha256={envelope['payload_sha256']}",
        f"  generated_at={envelope['generated_at']}",
        "status=fetched",
    ]
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    snapshot = await fetch_board_snapshot()
    envelope = build_envelope(snapshot, producer_git_sha=args.producer_git_sha)
    # 本地自校验：schema/hash/contract 一致性（不含敏感字段）
    validate_envelope(envelope)
    raw = serialize_envelope(envelope)

    if args.out:
        out_path = Path(args.out)
        out_path.write_bytes(raw)

    print(_summarize(snapshot, envelope, len(raw)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="board_snapshot_export",
        description="本地导出问财 board/concept 快照 envelope（供生产 importer 应用）",
    )
    parser.add_argument("--out", default=None, help="envelope 输出文件路径（UTF-8 JSON）")
    parser.add_argument(
        "--producer-git-sha", default=None, help="生产端 Git SHA（仅诊断）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="仅本地抓取 + 构造/校验，不写文件"
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        args.out = None
    try:
        return asyncio.run(_amain(args))
    except Exception as exc:  # noqa: BLE001 - 本地失败必须非零退出并给出可读原因
        print(f"status=failed\nlocal_error_code={type(exc).__name__}\nerror={exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
