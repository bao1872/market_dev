#!/usr/bin/env python3
"""比较当前 mypy JSONL 报告与基线，检测新增或增加的类型错误。

用法：
    python tools/compare_mypy_baseline.py <current-report.jsonl> <baseline.json>

退出码：
    0 - 没有新增或增加的诊断，且总数未超过基线
    1 - 发现新增/增加/总数超基线的诊断

规则：
- 当前出现基线没有的 (filename, error_code, message) 组合 -> 失败
- 同一组合的数量增加 -> 失败
- 总错误数超过基线 -> 失败
- 数量减少或消失 -> 允许
- 不允许通过全局 ignore、exclude 或批量 type: ignore 隐藏新增错误
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


def load_current_report(path: str) -> Counter[tuple[str, str, str]]:
    """加载 mypy JSONL 报告并按 (filename, error_code, message) 聚合计数。"""
    counter: Counter[tuple[str, str, str]] = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
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


def load_baseline(path: str) -> Counter[tuple[str, str, str]]:
    """加载基线文件中的诊断集合。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    counter: Counter[tuple[str, str, str]] = Counter()
    for diag in data.get("diagnostics", []):
        counter[
            (diag["filename"], diag["error_code"], diag["message"])
        ] = diag.get("count", 1)
    return counter


def compare(
    current: Counter[tuple[str, str, str]],
    baseline: Counter[tuple[str, str, str]],
) -> bool:
    """比较当前与基线，返回是否通过（无新增/增加/总数超限）。"""
    new_diags: list[tuple[tuple[str, str, str], int]] = []
    increased_diags: list[tuple[tuple[str, str, str], int, int]] = []
    fixed_diags: list[tuple[tuple[str, str, str], int, int]] = []

    for key, cur_count in sorted(current.items()):
        base_count = baseline.get(key, 0)
        if base_count == 0:
            new_diags.append((key, cur_count))
        elif cur_count > base_count:
            increased_diags.append((key, base_count, cur_count))

    for key, base_count in sorted(baseline.items()):
        cur_count = current.get(key, 0)
        if cur_count < base_count:
            fixed_diags.append((key, base_count, cur_count))

    total_current = sum(current.values())
    total_baseline = sum(baseline.values())

    print(f"Baseline raw error count: {total_baseline}")
    print(f"Current raw error count:  {total_current}")
    print(f"Baseline unique diagnostics: {len(baseline)}")
    print(f"Current unique diagnostics:  {len(current)}")

    if fixed_diags:
        print(f"\nFixed/reduced diagnostics ({len(fixed_diags)}):")
        for (fn, code, msg), base, cur in fixed_diags:
            print(f"  {fn}: {code} {base}->{cur} | {msg}")

    passed = True

    if total_current > total_baseline:
        print(
            f"\nFAIL: Total error count {total_current} exceeds baseline {total_baseline}."
        )
        passed = False

    if new_diags:
        print(f"\nNEW diagnostics not in baseline ({len(new_diags)}):")
        for (fn, code, msg), count in new_diags:
            print(f"  {fn}: {code} count={count} | {msg}")
        passed = False

    if increased_diags:
        print(f"\nINCREASED diagnostics ({len(increased_diags)}):")
        for (fn, code, msg), base, cur in increased_diags:
            print(f"  {fn}: {code} {base}->{cur} | {msg}")
        passed = False

    if passed:
        print("\nOK: No new or increased mypy diagnostics relative to baseline.")
    else:
        print(
            "\nFAIL: New or increased mypy diagnostics found. "
            "Fix them or update the baseline with justification."
        )

    return passed


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"Usage: {sys.argv[0]} <current-report.jsonl> <baseline.json>",
            file=sys.stderr,
        )
        return 2

    current_path, baseline_path = sys.argv[1], sys.argv[2]
    current = load_current_report(current_path)
    baseline = load_baseline(baseline_path)

    passed = compare(current, baseline)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
