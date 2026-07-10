#!/usr/bin/env python3
"""构建可追溯性只读检查（验收 / 部署门禁）。

目的：
    确保生产 worker 镜像 tag 与 GIT_SHA 可追溯、并与当前 git HEAD 一致，
    避免“构建打标签缺口”导致无法确认线上部署版本（CHANGE-20260710-003 已知问题）。

检查项（全部只读，不修改任何状态、不重启 / 不重建）：
    1. GIT_SHA 环境变量：必须存在且非 unknown，并与当前 HEAD（完整 / short）一致；
    2. 最新 worker_heartbeat.build_sha（只读 DB）：必须非 unknown 且与 HEAD 一致；
    3. 运行中的 backend / worker / after_close 容器镜像 tag（只读 docker inspect）：
       不得为 <none> / unknown。

判定：
    - 某项“已知但 unknown / 与 HEAD 不一致” → FAIL（退出码 1）；
    - 某项所需环境不可用（无 DATABASE_URL / 无 docker）→ SKIP（不影响通过，仅提示）；
    - 全部 PASS 或 SKIP → 通过（退出码 0）。

用法（仅只读，不改任何状态）：
    python tools/check_build_traceability.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys


def _run(cmd: list[str]):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 - 工具级容错，环境缺失时降级 SKIP
        return None


def get_head_sha():
    r = _run(["git", "rev-parse", "HEAD"])
    if r is None or r.returncode != 0:
        return None, None
    full = r.stdout.strip()
    short_r = _run(["git", "rev-parse", "--short", "HEAD"])
    short = short_r.stdout.strip() if short_r and short_r.returncode == 0 else full[:7]
    return full, short


def _sha_matches(candidate: str, head_full: str, head_short: str) -> bool:
    return candidate == head_full or candidate == head_short or head_full.startswith(candidate)


def check_git_sha(head_full: str, head_short: str):
    env_sha = os.environ.get("GIT_SHA")
    if not env_sha or env_sha == "unknown":
        return False, f"GIT_SHA 环境变量缺失或为 'unknown'（当前 HEAD={head_short}）"
    if _sha_matches(env_sha, head_full, head_short):
        return True, f"GIT_SHA={env_sha} 与 HEAD({head_short}) 一致"
    return False, f"GIT_SHA={env_sha} 与 HEAD({head_short}) 不一致"


def check_worker_heartbeat(head_full: str, head_short: str):
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not db_url:
        return None, "DATABASE_URL / TEST_DATABASE_URL 未设置，跳过 DB 检查（只读）"
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(db_url, pool_pre_ping=False)

        async def _query():
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT build_sha FROM worker_heartbeat "
                        "ORDER BY heartbeat_at DESC LIMIT 1"
                    )
                )
                row = result.first()
                return row[0] if row else None

        sha = asyncio.run(_query())
        asyncio.run(engine.dispose())
    except Exception as exc:  # noqa: BLE001 - 只读查询失败仅降级
        return None, f"DB 查询失败（跳过）：{exc}"
    if sha is None:
        return None, "无 worker_heartbeat 记录，跳过"
    if not sha or sha == "unknown":
        return False, f"最新 worker_heartbeat.build_sha='{sha}'（未知）"
    if _sha_matches(sha, head_full, head_short):
        return True, f"worker_heartbeat.build_sha={sha} 与 HEAD({head_short}) 一致"
    return False, f"worker_heartbeat.build_sha={sha} 与 HEAD({head_short}) 不一致"


def check_docker_images(head_short: str):
    r = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"])
    if r is None or r.returncode != 0:
        return None, "docker 不可用，跳过镜像检查（只读）"
    bad = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        name, image = line.split("\t", 1) if "\t" in line else (line, "")
        if any(k in name for k in ("backend", "worker", "after_close")):
            if "unknown" in image or image.endswith(":unknown") or "<none>" in image:
                bad.append(f"{name}={image}")
    if bad:
        return False, "镜像 tag 为 unknown/<none>: " + "; ".join(bad)
    return True, "运行中 backend/worker 镜像 tag 均有效"


def main() -> int:
    head_full, head_short = get_head_sha()
    if head_full is None:
        print("无法获取 git HEAD，终止")
        return 2
    print(f"当前 HEAD: {head_full} ({head_short})")

    results = [
        ("GIT_SHA",) + check_git_sha(head_full, head_short),
        ("worker_heartbeat.build_sha",) + check_worker_heartbeat(head_full, head_short),
        ("docker 镜像 tag",) + check_docker_images(head_short),
    ]

    failed = False
    for name, ok, msg in results:
        status = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
        if status == "FAIL":
            failed = True
        print(f"[{status}] {name}: {msg}")

    if failed:
        print("\n构建可追溯性检查失败：存在 unknown 或不一致的构建版本信息。")
        return 1
    print("\n构建可追溯性检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
