#!/usr/bin/env python3
"""根据指定 Commit 的 mypy JSON 输出生成基线文件。

用法：
    /tmp/mypy_venv/bin/python -m mypy app \
        --output json --show-error-codes --no-error-summary \
        --hide-error-context --no-pretty \
        | python tools/generate_mypy_baseline.py <baseline_commit> > tools/quality_baselines/mypy.json

或从文件读取：
    python tools/generate_mypy_baseline.py <baseline_commit> <mypy-report.jsonl>

基线格式（JSON）：
    {
      "baseline_commit": "<sha>",
      "total": <int>,
      "diagnostics": [
        {"filename": "app/foo.py", "error_code": "attr-defined",
         "message": "...", "count": 1}
      ]
    }
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def _normalize_filename(fn: str) -> str:
    """统一路径为 backend 根目录下的相对路径。"""
    if fn.startswith("backend/"):
        return fn[len("backend/") :]
    if "/backend/" in fn and not fn.startswith("backend/"):
        return fn.split("/backend/", 1)[1]
    return fn


def load_current_report(stream) -> Counter[tuple[str, str, str]]:
    """读取 mypy JSONL 报告并按 (filename, error_code, message) 聚合计数。"""
    counter: Counter[tuple[str, str, str]] = Counter()
    for line in stream:
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if item.get("severity") != "error":
            continue
        fn = _normalize_filename(item.get("file", ""))
        code = item.get("code") or "unknown"
        message = item.get("message", "")
        counter[(fn, code, message)] += 1
    return counter


def build_baseline(commit: str, counter: Counter[tuple[str, str, str]]) -> dict:
    """构造基线 JSON。"""
    diagnostics = [
        {
            "filename": fn,
            "error_code": code,
            "message": msg,
            "count": count,
        }
        for (fn, code, msg), count in sorted(counter.items())
    ]
    return {
        "baseline_commit": commit,
        "total": sum(counter.values()),
        "unique": len(counter),
        "diagnostics": diagnostics,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(
            f"Usage: {sys.argv[0]} <baseline_commit> [mypy-report.jsonl]",
            file=sys.stderr,
        )
        return 2

    baseline_commit = sys.argv[1]
    if len(sys.argv) >= 3:
        input_path = Path(sys.argv[2])
        stream = input_path.open(encoding="utf-8")
    else:
        stream = sys.stdin

    counter = load_current_report(stream)
    baseline = build_baseline(baseline_commit, counter)
    print(json.dumps(baseline, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
