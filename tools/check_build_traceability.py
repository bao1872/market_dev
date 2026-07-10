#!/usr/bin/env python3
"""构建可追溯性只读检查（验收 / 部署门禁）。

目的：
    确保生产 worker 镜像 tag 与 GIT_SHA 可追溯、并与当前 git HEAD 一致，
    避免“构建打标签缺口”导致无法确认线上部署版本（CHANGE-20260710-003 已知问题）。

检查项（全部只读，不修改任何状态、不重启 / 不重建）：
    1. GIT_SHA 环境变量：必须存在且非 unknown，并与当前 HEAD（完整 / short）一致；
    2. 每个必需 worker 的最新心跳（worker_heartbeats 表，按 worker_name 取最新一条）：
       running 状态 worker 的 build_sha 必须非 unknown 且与 HEAD 一致；
    3. 运行中 backend / worker / after_close / capture / delivery / outbox 容器镜像：
       镜像 tag 不得为 <none> / unknown，且必须从 image tag 或 OCI revision label
       中解析出与 HEAD 一致的 SHA。

判定（P0-4 修正）：
    - 某项“未知但 unknown / 与 HEAD 不一致 / 解析缺失” → FAIL（退出码 1）；
    - 某项所需环境不可用（无 DATABASE_URL / 无 docker）：
        * 默认 --strict（生产）：必须 FAIL；
        * 本地显式 --allow-skip：SKIP（不影响通过，仅提示）；
    - 全部 PASS 或 SKIP → 通过（退出码 0）。

用法（仅只读，不改任何状态）：
    python tools/check_build_traceability.py                 # 默认 --strict
    python tools/check_build_traceability.py --allow-skip   # 本地调试
    python -m pytest tools/tests/test_check_build_traceability.py  # 纯函数单测
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from typing import Any

# 心跳新鲜度阈值（秒）：running worker 的心跳在该阈值内视为“新鲜”。
# 注：本工具只要求 running worker 的 build_sha 可追溯，不再对心跳新鲜度做硬判定，
# 因为部署门禁关心的是“构建版本是否一致”，心跳新鲜度由 worker-watchdog 独立负责。
_HEARTBEAT_FRESH_SECONDS = 600

# 候选 SHA 最小长度：短 SHA 默认 7 位，少于 7 位不足以可靠匹配 HEAD。
_MIN_SHA_LEN = 7


def _run(cmd: list[str]):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 - 工具级容错，环境缺失时降级
        return None


def get_head_sha():
    r = _run(["git", "rev-parse", "HEAD"])
    if r is None or r.returncode != 0:
        return None, None
    full = r.stdout.strip()
    short_r = _run(["git", "rev-parse", "--short", "HEAD"])
    short = short_r.stdout.strip() if short_r and short_r.returncode == 0 else full[:7]
    return full, short


def _sha_matches(candidate: str | None, head_full: str, head_short: str) -> bool:
    """候选 SHA 是否与 HEAD 一致（候选至少 7 位才参与匹配）。"""
    if not candidate or len(candidate) < _MIN_SHA_LEN:
        return False
    return candidate == head_full or candidate == head_short or head_full.startswith(candidate)


def check_git_sha(head_full: str, head_short: str):
    env_sha = os.environ.get("GIT_SHA")
    if not env_sha or env_sha == "unknown":
        return False, f"GIT_SHA 环境变量缺失或为 'unknown'（当前 HEAD={head_short}）"
    if _sha_matches(env_sha, head_full, head_short):
        return True, f"GIT_SHA={env_sha} 与 HEAD({head_short}) 一致"
    return False, f"GIT_SHA={env_sha} 与 HEAD({head_short}) 不一致"


def classify_worker_heartbeat(
    worker_name: str,
    status: str | None,
    build_sha: str | None,
    head_full: str,
    head_short: str,
) -> tuple[bool | None, str]:
    """对单条 worker 最新心跳分类（纯函数，便于单测）。

    Returns:
        (ok, reason)：
        - ok=True：通过（running 且 build_sha 匹配 HEAD，或非 running 不要求）；
        - ok=False：失败（running 但 build_sha unknown / 不匹配 HEAD）；
        - ok=None：不要求（非 running 状态，跳过）。
    """
    if status != "running":
        return None, f"worker={worker_name} 状态={status or 'unknown'}（非 running，不要求版本匹配）"
    if not build_sha or build_sha == "unknown":
        return False, f"worker={worker_name} 为 running 但 build_sha='{build_sha}'（未知）"
    if _sha_matches(build_sha, head_full, head_short):
        return True, f"worker={worker_name} running 且 build_sha={build_sha} 与 HEAD({head_short}) 一致"
    return False, f"worker={worker_name} running 但 build_sha={build_sha} 与 HEAD({head_short}) 不一致"


def parse_docker_inspect(container_json: dict[str, Any]) -> tuple[str | None, str | None]:
    """从单条 docker inspect JSON 解析镜像 tag 与 OCI revision label（纯函数）。"""
    config = container_json.get("Config") or {}
    image_tag = config.get("Image")
    labels = config.get("Labels") or {}
    # 常见 OCI revision label 键
    revision_label = (
        labels.get("org.opencontainers.image.revision")
        or labels.get("org.label-schema.vcs-ref")
        or labels.get("vcs-ref")
        or labels.get("revision")
    )
    return image_tag, revision_label


def _extract_tag_segment(image_ref: str) -> str:
    """从镜像引用中提取用于匹配 SHA 的 tag/digest 段。

    支持形式：
        name:tag                 -> tag
        registry:5000/name:tag   -> tag（最后一个冒号后）
        name@sha256:<digest>     -> <digest>
        name                     -> name（无 tag，整串）
    """
    ref = image_ref.strip()
    if "@" in ref:
        # digest 形式：name@sha256:<hex> -> 取 <hex>
        digest = ref.split("@", 1)[1]
        return digest.rsplit(":", 1)[-1]
    # 若含 "/"，冒号可能属于 registry 端口，只在最后一段找 tag
    last_component = ref.rsplit("/", 1)[-1]
    if ":" in last_component:
        return last_component.rsplit(":", 1)[-1]
    return last_component


def check_docker_image(
    image_tag: str | None,
    revision_label: str | None,
    head_full: str,
    head_short: str,
) -> tuple[bool, str]:
    """校验单个容器镜像 tag / OCI revision 是否与 HEAD 一致（纯函数）。

    tag 匹配：提取镜像引用最后一段的 tag/digest，再与 HEAD 比对；
    label 匹配：OCI revision label 直接与 HEAD 比对。
    """
    if not image_tag or image_tag == "unknown" or "<none>" in image_tag or image_tag.endswith(":unknown"):
        return False, f"镜像 tag 为 unknown/<none>: {image_tag!r}"
    # 非 unknown 但必须从 tag 段或 OCI revision label 解析出与 HEAD 一致的 SHA
    tag_segment = _extract_tag_segment(image_tag)
    tag_ok = _sha_matches(tag_segment, head_full, head_short)
    label_ok = _sha_matches(revision_label, head_full, head_short)
    if tag_ok:
        return True, f"镜像 tag={image_tag}（tag 段 {tag_segment}）与 HEAD({head_short}) 一致"
    if label_ok:
        return True, f"镜像 OCI revision label={revision_label} 与 HEAD({head_short}) 一致"
    return False, f"镜像 tag={image_tag} 与 OCI revision label={revision_label!r} 均无法匹配 HEAD({head_short})"


def check_docker_images(head_full: str, head_short: str, allow_skip: bool):
    """检查运行中相关容器镜像（只读 docker inspect）。"""
    r = _run(["docker", "ps", "--format", "{{.Names}}\t{{.ID}}"])
    if r is None or r.returncode != 0:
        if allow_skip:
            return None, "docker 不可用，跳过镜像检查（--allow-skip）"
        return False, "docker 不可用，但处于 --strict 模式，镜像检查必须执行"
    if not r.stdout.strip():
        return None, "无运行中容器，跳过镜像检查"

    results: list[tuple[str, bool, str]] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0]
        cid = parts[1] if len(parts) > 1 else name
        if not any(k in name for k in ("backend", "worker", "after_close", "capture", "delivery", "outbox")):
            continue
        insp = _run(["docker", "inspect", cid, "--format", "{{json .}}"])
        if insp is None or insp.returncode != 0 or not insp.stdout.strip():
            results.append((name, False, "docker inspect 失败"))
            continue
        try:
            cj = json.loads(insp.stdout)
        except json.JSONDecodeError:
            results.append((name, False, "docker inspect 输出非 JSON"))
            continue
        image_tag, revision_label = parse_docker_inspect(cj)
        ok, reason = check_docker_image(image_tag, revision_label, head_full, head_short)
        results.append((name, ok, reason))

    if not results:
        return None, "无 backend/worker 相关容器，跳过镜像检查"
    failed = [f"{n}: {rsn}" for n, ok, rsn in results if not ok]
    if failed:
        return False, "镜像版本不一致: " + "; ".join(failed)
    return True, "运行中 backend/worker 镜像均与 HEAD 一致"


async def _query_worker_heartbeats(db_url: str) -> list[dict[str, Any]]:
    """查询每个 worker 的最新一条心跳（按 worker_name 去重取最新）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url, pool_pre_ping=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT DISTINCT ON (worker_name) worker_name, status, build_sha, heartbeat_at "
                    "FROM worker_heartbeats "
                    "ORDER BY worker_name, heartbeat_at DESC"
                )
            )
            rows = [
                {
                    "worker_name": row[0],
                    "status": row[1],
                    "build_sha": row[2],
                    "heartbeat_at": row[3],
                }
                for row in result.fetchall()
            ]
        return rows
    finally:
        await engine.dispose()


def check_worker_heartbeats(head_full: str, head_short: str, allow_skip: bool):
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not db_url:
        if allow_skip:
            return None, "DATABASE_URL / TEST_DATABASE_URL 未设置，跳过 DB 心跳检查（--allow-skip）"
        return False, "DATABASE_URL / TEST_DATABASE_URL 未设置，但处于 --strict 模式，心跳检查必须执行"
    try:
        rows = asyncio.run(_query_worker_heartbeats(db_url))
    except Exception as exc:  # noqa: BLE001 - DB 不可用按门禁处理
        if allow_skip:
            return None, f"DB 查询失败（跳过）：{exc}"
        return False, f"DB 查询失败（--strict 必须执行）：{exc}"

    if not rows:
        # 无任何心跳记录：视为无法验证（按门禁要求 FAIL，除非 allow_skip）
        if allow_skip:
            return None, "无 worker_heartbeats 记录，跳过（--allow-skip）"
        return False, "无 worker_heartbeats 记录，无法验证构建版本可追溯性（--strict）"

    classified = [
        (row["worker_name"],) + classify_worker_heartbeat(
            row["worker_name"], row["status"], row["build_sha"], head_full, head_short
        )
        for row in rows
    ]
    failed = [f"{w}: {rsn}" for w, ok, rsn in classified if ok is False]
    if failed:
        return False, "worker 构建版本不一致: " + "; ".join(failed)
    return True, f"全部 {len(rows)} 个 worker 心跳版本可追溯（running 均匹配 HEAD）"


def main() -> int:
    parser = argparse.ArgumentParser(description="构建可追溯性只读检查")
    parser.add_argument(
        "--allow-skip",
        action="store_true",
        help="本地调试：DB/Docker 不可用则 SKIP；默认 --strict（生产）必须执行",
    )
    args = parser.parse_args()
    allow_skip = args.allow_skip

    head_full, head_short = get_head_sha()
    if head_full is None:
        print("无法获取 git HEAD，终止")
        return 2
    print(f"当前 HEAD: {head_full} ({head_short})")

    results = [
        ("GIT_SHA",) + check_git_sha(head_full, head_short),
        ("worker_heartbeats.build_sha",) + check_worker_heartbeats(head_full, head_short, allow_skip),
        ("docker 镜像 tag/OCI label",) + check_docker_images(head_full, head_short, allow_skip),
    ]

    failed = False
    for name, ok, msg in results:
        if ok is True:
            status = "PASS"
        elif ok is None:
            status = "SKIP"
        else:
            status = "FAIL"
            failed = True
        print(f"[{status}] {name}: {msg}")

    if failed:
        print("\n构建可追溯性检查失败：存在 unknown 或不一致的构建版本信息。")
        return 1
    print("\n构建可追溯性检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
