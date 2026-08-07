#!/usr/bin/env python3
"""V2.1 验证尝试精确清理执行器（CHANGE-20260806-008, Phase 2）。

[DS-110 / DS-112 / rules/80] 清理必须按 attempt manifest 精确删除本次尝试创建的资源，
永久保护清单硬编码拒绝 bz_stock / postgres / template* / 共享 Volume / trading-* 容器 /
基础镜像 / 来源不明资源；禁止全局 prune 与模糊 DB drop。清理失败标记 blocked_cleanup
并停止新建资源（等价用户计划 §16 finally 合同的 cleanup 分支）。

本模块被 verify_attempt.py 的 finally 调用，也可在超时/中断后由运维手工单独调用
（传入 attempt manifest 路径）完成补偿清理。

用法：
  python scripts/verify/cleanup_runner.py --manifest /path/to/attempt_manifest.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("verify.cleanup")

# ---------------------------------------------------------------------------
# 永久保护清单（硬编码，任何清理都不得触碰）
# ---------------------------------------------------------------------------

PROTECTED_CONTAINERS = {
    "trading-postgres",
    "trading-redis",
    "verify-postgres",
    "verify-redis",
    "web_dev",
    "web_dev_verify",
    # [CHANGE-20260806-012] 单可复用验证运行时：常驻容器与固定 project 纵深防御
    "panji-verify-python",
}

PROTECTED_IMAGES = {
    "panji-backend",
    "panji-frontend",
    "node",
    "python",
    "postgres",
    "redis",
    "nginx",
}

PROTECTED_DATABASES = {
    "postgres",
    "bz_stock",
    "template0",
    "template1",
}

# 任何以这些前缀开头的容器名/镜像名/库名也受保护
PROTECTED_PREFIXES = (
    "trading-",
    "web_dev",
    "panji-prod",
    "main-",
    # [CHANGE-20260806-012] 固定 project panji-verify 常驻，禁止清理
    "panji-verify",
)

CLEANUP_JSON_NAME = "cleanup.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_protected(name: str, kind: str) -> bool:
    """检查名称是否在永久保护清单内（精确匹配或前缀匹配）。"""
    if (
        (kind == "container" and name in PROTECTED_CONTAINERS)
        or (kind == "image" and name in PROTECTED_IMAGES)
        or (kind == "database" and name in PROTECTED_DATABASES)
    ):
        return True
    for prefix in PROTECTED_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _verify_db_re() -> re.Pattern:
    return re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")


def _run(cmd: list[str], *, check: bool = False) -> tuple[int, str, str]:
    """运行子命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "command timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _compose_project(manifest: dict) -> str | None:
    return manifest.get("compose_project")


def _safe_drop_database(db_name: str) -> dict:
    """精确 drop 验证库（仅 bz_stock_verify_<sha>）。

    fail-closed：库名非法或命中保护清单则拒绝。删除只通过既有 PostgreSQL 容器执行。
    """
    result: dict[str, Any] = {"database": db_name, "dropped": False, "error": None}
    if not _verify_db_re().match(db_name):
        result["error"] = f"非法验证库名 '{db_name}'（必须 bz_stock_verify_<sha>）"
        return result
    if _is_protected(db_name, "database"):
        result["error"] = f"库名 '{db_name}' 命中永久保护清单，拒绝删除"
        return result
    code, _out, err = _run([
        "docker", "exec", "trading-postgres", "psql", "-U", "bz", "-d", "postgres",
        "-v", "ON_ERROR_STOP=1", "-c",
        f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);',
    ])
    if code != 0:
        result["error"] = f"drop database 失败: {err.strip()}"
        return result
    result["dropped"] = True
    return result


def cleanup_attempt(manifest_path: str | Path) -> dict:
    """按 attempt manifest 精确清理。返回 cleanup 结果 dict（写入 cleanup.json）。

    步骤：
      1. 读取 manifest，校验 attempt_id / verify_database / compose_project 齐全；
      2. 删除本次尝试创建的 compose 容器和 network（永不带 -v）；
      3. 精确 drop 验证库（bz_stock_verify_<sha>，双校验）；
      4. 保留受限 evidence，临时运行目录由框架单独精确删除；
      5. 任一关键步骤失败 → blocked_cleanup=True，记录失败项但不继续新建资源。
    """
    manifest_path = Path(manifest_path)
    summary: dict[str, Any] = {
        "attempt_id": None,
        "cleaned_at": _utcnow(),
        "dropped_database": None,
        "removed_compose_project": False,
        "removed_evidence_dir": False,
        "blocked_cleanup": False,
        "blocked_reasons": [],
        "errors": [],
    }

    if not manifest_path.exists():
        summary["blocked_cleanup"] = True
        summary["blocked_reasons"].append(f"manifest 不存在: {manifest_path}")
        return summary

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:  # noqa: BLE001
        summary["blocked_cleanup"] = True
        summary["blocked_reasons"].append(f"manifest 解析失败: {exc}")
        return summary

    summary["attempt_id"] = manifest.get("attempt_id")
    verify_database = manifest.get("verify_database")
    compose_project = manifest.get("compose_project")
    evidence_dir = manifest.get("evidence_dir")

    # [CHANGE-20260806-012] 减法重构：单可复用运行时（固定 project panji-verify + 常驻
    # panji-verify-python 容器）不在 attempt cleanup 中删除、不执行 compose 删除命令、不删 Volume、
    # 不 FLUSHALL。cleanup 只负责 attempt 精确资源：精确 drop 验证库 + 标记状态。
    # 常驻容器的临时执行状态恢复由 verify_attempt.recover_container（docker restart）负责。
    if compose_project:
        if compose_project == "panji-verify":
            # [CHANGE-20260806-012] 单可复用运行时：固定 project 常驻，不删、不算 cleanup failure
            pass
        elif _is_protected(compose_project, "container"):
            # 其他受保护 project：拒绝删除（纵深防御）
            summary["blocked_cleanup"] = True
            summary["blocked_reasons"].append(
                f"compose project '{compose_project}' 受保护，拒绝删除常驻栈（防误删）"
            )
        else:
            # 任何非常驻/未登记 compose project 都不允许 cleanup 触碰（纵深防御）
            summary["blocked_cleanup"] = True
            summary["blocked_reasons"].append(
                f"compose project '{compose_project}' 未登记为可清理，拒绝删除"
            )

    # 1) 精确 drop 验证库（manifest 精确库名 + 永久保护清单）
    if verify_database:
        db_result = _safe_drop_database(verify_database)
        summary["dropped_database"] = db_result
        if not db_result.get("dropped") and db_result.get("error"):
            summary["errors"].append(db_result["error"])

    if summary["errors"] or summary["blocked_reasons"]:
        summary["blocked_cleanup"] = True

    # Evidence is intentionally retained under a strict size budget for retrieval.
    out_dir = Path(evidence_dir) if evidence_dir else manifest_path.parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / CLEANUP_JSON_NAME).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 cleanup.json 失败（尽力而为，不影响主流程）: %s", exc)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="attempt manifest JSON 路径")
    args = ap.parse_args()
    summary = cleanup_attempt(args.manifest)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 70 if summary["blocked_cleanup"] else 0


if __name__ == "__main__":
    sys.exit(main())
