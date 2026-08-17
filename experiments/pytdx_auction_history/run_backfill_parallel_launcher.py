"""Auction 120-bar 并行回补 launcher — 按连续 bar 区间分 worker，保留 global bar_index。

设计（对齐审查报告 ref/竞价.md 第 4/5/10 节）：
- 不按 symbol 分片，而按连续 bar 区间分 worker：同一 worker 内保留跨日 offset hints
  （warm-start）。
- 每个 worker 是独立子进程 + 独立 PytdxAdapter 连接 + 独立 run-id / output-root。
- 每个 worker 的 --bar-range 是**全局 1-based bar 序号**闭区间（如 worker2 第一根 = 31），
  由 runner 内 resolve_bar_range 基于同一官方 120 bar 日历解析，保证 bar_index 连续。
- N 值默认 2，禁止把未入库 dry benchmark 的 N=8 当合同；由 Canary（阶段 4）决定后显式传入。
- 聚合 manifest 必须包含 Σ db_written_rows / Σ db_failed_rows / 失败 bar 清单
  （root manifest 不累计 DB metrics，审查报告 §10）。

运行示例：
    python experiments/pytdx_auction_history/run_backfill_parallel_launcher.py \
        --as-of 2026-08-14 --workers 2 --bar-range "1:4" --write-db \
        --run-id-prefix member_fact_120bar_pg
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from app.db import AsyncSessionLocal  # noqa: E402

from auction_history_semantics_validation import previous_trading_dates  # noqa: E402
from full_market_member_fact_backfill import (  # noqa: E402
    BAR_COUNT,
    parse_bar_range,
    resolve_bar_range,
)

# 允许并发 worker 的默认值：保守，由 Canary 现场 evidence 决定是否上调。
DEFAULT_WORKERS = 2
POLL_INTERVAL_SEC = 5.0

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"


# ---------------------------------------------------------------------------
# 纯函数（可单测）
# ---------------------------------------------------------------------------
def slice_into_ranges(total: int, workers: int) -> list[tuple[int, int]]:
    """把 1..total 切成 workers 个连续、不重叠、覆盖全量的闭区间。

    Args:
        total: 要处理的 bar 总数（>= 1）。
        workers: 并发 worker 数（>= 1，<= total）。

    Returns:
        [(start, end), ...]（1-based 闭区间），区间首尾相接覆盖 [1, total]。

    Raises:
        ValueError: total < 1 或 workers < 1 或 workers > total（fail fast）。
    """
    if total < 1:
        raise ValueError(f"total must be >= 1, got {total}")
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if workers > total:
        raise ValueError(f"workers {workers} > total bars {total}（每 worker 至少 1 bar）")
    base, rem = divmod(total, workers)
    ranges: list[tuple[int, int]] = []
    cur = 1
    for i in range(workers):
        size = base + (1 if i < rem else 0)
        ranges.append((cur, cur + size - 1))
        cur += size
    assert cur - 1 == total, f"slice must cover [1,{total}], ended at {cur - 1}"
    return ranges


def worker_run_id(prefix: str, index: int, workers: int) -> str:
    """生成 worker 独立 run-id：`{prefix}_worker_{index:02d}`。"""
    if index < 1 or index > workers:
        raise ValueError(f"worker index {index} out of range [1,{workers}]")
    return f"{prefix}_worker_{index:02d}"


def build_worker_command(
    runner_script: str,
    *,
    mode: str,
    start: int,
    end: int,
    as_of: date,
    run_id: str,
    output_root: str,
    write_db: bool,
    mdas_chunk_size: int,
    python: str | None = None,
) -> list[str]:
    """拼装单个 worker 子进程命令（global bar-range + 独立 run-id / output-root）。"""
    python = python or sys.executable
    cmd = [
        python, runner_script,
        "--mode", mode,
        "--bar-range", f"{start}:{end}",
        "--run-id", run_id,
        "--output-root", output_root,
        "--as-of", as_of.isoformat(),
        "--mdas-chunk-size", str(mdas_chunk_size),
    ]
    if write_db:
        cmd.append("--write-db")
    return cmd


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def aggregate_worker(root_manifest: dict[str, Any] | None,
                     partition_manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """把单个 worker 的 root manifest + 各 partition manifest 聚合为一行指标。"""
    completed = 0
    failed = 0
    db_written = 0
    db_failed = 0
    failed_bars: list[str] = []
    for pm in partition_manifests:
        status = pm.get("status")
        if status == "COMPLETED":
            completed += 1
        elif status == "FAILED":
            failed += 1
            td = pm.get("trade_date")
            if td is not None:
                failed_bars.append(str(td))
        db_written += int(pm.get("db_written_rows", 0) or 0)
        db_failed += int(pm.get("db_failed_rows", 0) or 0)
    return {
        "root_status": (root_manifest or {}).get("status", "UNKNOWN"),
        "completed_bar_count": completed,
        "failed_bar_count": failed,
        "db_written_rows": db_written,
        "db_failed_rows": db_failed,
        "failed_bars": failed_bars,
    }


def collect_worker_outputs(output_root: Path, run_id: str) -> tuple[dict[str, Any] | None,
                                                                    list[dict[str, Any]]]:
    """读取 worker 的 root manifest 与全部 partition manifests。"""
    run_dir = output_root / run_id
    root = _load_json(run_dir / "manifest.json")
    partitions: list[dict[str, Any]] = []
    if run_dir.is_dir():
        for pm_path in sorted(run_dir.glob("*/partition_manifest.json")):
            pm = _load_json(pm_path)
            if pm is not None:
                partitions.append(pm)
    return root, partitions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_backfill_parallel_launcher",
        description="Auction 120-bar 并行回补 launcher（连续 bar 区间分 worker，global bar_index）。",
    )
    p.add_argument("--as-of", default="2026-08-14", help="as_of date（默认 2026-08-14）")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"并发 worker 数（默认 {DEFAULT_WORKERS}，由 Canary 现场 evidence 决定上调）")
    p.add_argument("--bar-range", default=None,
                   help="可选：全局 1-based bar 闭区间 'START:END'（默认全量官方 bars）")
    p.add_argument("--write-db", action="store_true",
                   help="显式传给 worker 的 --write-db（写真实业务库必须显式授权）")
    p.add_argument("--run-id-prefix", default="member_fact_120bar_pg")
    p.add_argument("--output-root", default=None,
                   help="output root（默认 runner 的 OUTPUT_DIR）")
    p.add_argument("--runner", default=None,
                   help="runner 脚本路径（默认 experiments/.../full_market_member_fact_backfill.py）")
    p.add_argument("--mdas-chunk-size", type=int, default=512)
    p.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SEC)
    return p


def _resolve_output_root(launcher_dir: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return launcher_dir / "output" / "member_fact_120bar" / "2026-08-14"


def _default_runner(launcher_dir: Path) -> Path:
    return launcher_dir / "full_market_member_fact_backfill.py"


def _spawn_worker(cmd: list[str], run_id: str, output_root: Path) -> subprocess.Popen:
    log_dir = output_root / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "worker.stdout.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115
    # 注入 backend 到子进程 PYTHONPATH，保证 runner 内 `from app...` 可解析。
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_BACKEND_DIR) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env,
        cwd=str(Path(cmd[1]).resolve().parent),
    )


async def _monitor_workers(procs: list[tuple[str, subprocess.Popen]],
                           output_root: Path,
                           poll_interval: float) -> dict[str, dict[str, Any]]:
    """轮询 worker 完成 + 聚合各 worker 的 DB metrics。"""
    results: dict[str, dict[str, Any]] = {}
    remaining = {run_id: proc for run_id, proc in procs}
    while remaining:
        for run_id, proc in list(remaining.items()):
            if proc.poll() is not None:
                root, partitions = collect_worker_outputs(output_root, run_id)
                results[run_id] = aggregate_worker(root, partitions)
                results[run_id]["exit_code"] = proc.returncode
                remaining.pop(run_id)
        if remaining:
            await asyncio.sleep(poll_interval)
    return results


async def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    as_of = date.fromisoformat(args.as_of)
    launcher_dir = Path(__file__).resolve().parent
    runner_script = str(args.runner or _default_runner(launcher_dir))
    output_root = _resolve_output_root(launcher_dir, args.output_root)

    async with AsyncSessionLocal() as session:
        # 共享 helper：官方日历 + 可选 launcher 级 bar-range 切片（同 runner 口径）
        bar_dates, base_offset = await resolve_bar_range(
            session, as_of, args.bar_range, BAR_COUNT)
    total = len(bar_dates)
    start_global = base_offset
    end_global = base_offset + total - 1

    local_ranges = slice_into_ranges(total, args.workers)
    global_ranges = [(start_global + ls - 1, start_global + le - 1)
                     for ls, le in local_ranges]

    print(f"[LAUNCHER] as_of={as_of.isoformat()} bars={total} "
          f"global_range=[{start_global},{end_global}] workers={args.workers} "
          f"write_db={args.write_db}")
    procs: list[tuple[str, subprocess.Popen]] = []
    for i, (gs, ge) in enumerate(global_ranges, start=1):
        run_id = worker_run_id(args.run_id_prefix, i, args.workers)
        cmd = build_worker_command(
            runner_script,
            mode="live", start=gs, end=ge, as_of=as_of,
            run_id=run_id, output_root=str(output_root),
            write_db=args.write_db, mdas_chunk_size=args.mdas_chunk_size,
        )
        print(f"[LAUNCHER] worker{i} run_id={run_id} --bar-range {gs}:{ge}")
        procs.append((run_id, _spawn_worker(cmd, run_id, output_root)))

    workers_detail = await _monitor_workers(procs, output_root, args.poll_interval)

    totals = {"bars_completed": 0, "bars_failed": 0,
              "db_written_rows": 0, "db_failed_rows": 0, "failed_bars": []}
    for run_id, detail in workers_detail.items():
        totals["bars_completed"] += detail.get("completed_bar_count", 0)
        totals["bars_failed"] += detail.get("failed_bar_count", 0)
        totals["db_written_rows"] += detail.get("db_written_rows", 0)
        totals["db_failed_rows"] += detail.get("db_failed_rows", 0)
        totals["failed_bars"] += detail.get("failed_bars", [])

    aggregate = {
        "as_of": as_of.isoformat(),
        "workers": args.workers,
        "global_bar_range": f"{start_global}:{end_global}",
        "write_db": args.write_db,
        "workers_detail": workers_detail,
        "totals": totals,
    }
    agg_path = output_root / "launcher_manifest.json"
    agg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(agg_path, "w", encoding="utf-8") as f:
        json.dump(aggregate, f, ensure_ascii=False, indent=2)
    print(f"[LAUNCHER] aggregate manifest -> {agg_path}")
    print(f"[LAUNCHER] totals={json.dumps(totals, ensure_ascii=False)}")
    return 0 if totals["bars_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
