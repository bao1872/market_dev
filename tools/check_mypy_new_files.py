#!/usr/bin/env python3
"""检查新增 production Python 文件是否有 mypy 错误。

用法：
    python tools/check_mypy_new_files.py <base_sha> <head_sha> <mypy-report.jsonl>

或从 stdin 读取报告：
    python tools/check_mypy_new_files.py <base_sha> <head_sha> -

只检查 backend/app 下新增（diff-filter=A）的 .py 文件；测试文件不纳入阻断。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _normalize_filename(fn: str) -> str:
    """统一路径为 backend 根目录下的相对路径。"""
    if fn.startswith("backend/"):
        return fn[len("backend/") :]
    if "/backend/" in fn and not fn.startswith("backend/"):
        return fn.split("/backend/", 1)[1]
    return fn


def get_new_app_files(base_sha: str, head_sha: str) -> set[str]:
    """返回 base..head 之间 backend/app 下新增的 .py 文件相对路径集合。"""
    cmd = [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=A",
        base_sha,
        head_sha,
        "--",
        "backend/app/*.py",
        "backend/app/**/*.py",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    files = set()
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        files.add(_normalize_filename(line))
    return files


def check_report(new_files: set[str], report_path: str) -> bool:
    """检查报告中是否有新增文件的 mypy error。"""
    if report_path == "-":
        stream = sys.stdin
    else:
        stream = open(report_path, encoding="utf-8")

    found: list[dict] = []
    with stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("severity") != "error":
                continue
            fn = _normalize_filename(item.get("file", ""))
            if fn in new_files:
                found.append(item)

    if not found:
        print(f"No mypy errors in {len(new_files)} new app file(s).")
        return True

    print(f"FAIL: Found {len(found)} mypy error(s) in new app file(s):")
    for item in found:
        print(
            f"  {item.get('file')}:{item.get('line')} "
            f"[{item.get('code')}] {item.get('message')}"
        )
    return False


def main() -> int:
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <base_sha> <head_sha> <mypy-report.jsonl|-",
            file=sys.stderr,
        )
        return 2

    base_sha, head_sha, report_path = sys.argv[1], sys.argv[2], sys.argv[3]
    new_files = get_new_app_files(base_sha, head_sha)
    if not new_files:
        print("No new app Python files in this change.")
        return 0

    print(f"New app Python files ({len(new_files)}):")
    for fn in sorted(new_files):
        print(f"  {fn}")

    passed = check_report(new_files, report_path)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
