#!/usr/bin/env python3
"""比较当前 Ruff JSON 报告与基线，检测新增或增加的历史债务。

用法：
    python tools/compare_ruff_baseline.py <current-report.json> <baseline.json>

退出码：
    0 - 没有新增或增加的诊断
    1 - 发现新增或增加的诊断

规则：
- 当前出现基线没有的 (filename, code, message) 组合 -> 失败
- 同一组合的数量增加 -> 失败
- 数量减少或消失 -> 允许
- 不允许通过 noqa、全局 ignore、per-file-ignore 或 exclude 批量隐藏
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def load_diagnostics(path: str) -> Counter[tuple[str, str, str]]:
    """加载 Ruff JSON 报告并按 (filename, code, message) 聚合计数。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    counter: Counter[tuple[str, str, str]] = Counter()
    for item in data:
        fn = item["filename"]
        # CI 中可能在 backend 目录运行，filename 形如 backend/alembic/versions/...
        # 统一去掉 backend/ 前缀，保证与基线一致
        if fn.startswith("backend/"):
            fn = fn[len("backend/"):]
        # 去掉可能出现的 /tmp/... 前缀（本地 worktree 场景）
        if "/backend/" in fn and not fn.startswith("backend/"):
            fn = fn.split("/backend/", 1)[1]
        counter[(fn, item["code"], item["message"])] += 1
    return counter


def load_baseline(path: str) -> Counter[tuple[str, str, str]]:
    """加载基线文件中的诊断集合。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    counter: Counter[tuple[str, str, str]] = Counter()
    for diag in data.get("diagnostics", []):
        counter[(diag["filename"], diag["code"], diag["message"])] = diag.get("count", 1)
    return counter


def compare(current: Counter[tuple[str, str, str]], baseline: Counter[tuple[str, str, str]]) -> bool:
    """比较当前与基线，返回是否通过（无新增/增加）。"""
    new_diags: list[tuple[tuple[str, str, str], int]] = []
    increased_diags: list[tuple[tuple[str, str, str], int, int]] = []
    fixed_diags: list[tuple[tuple[str, str, str], int, int]] = []

    # 当前存在但基线没有，或数量增加
    for key, cur_count in sorted(current.items()):
        base_count = baseline.get(key, 0)
        if base_count == 0:
            new_diags.append((key, cur_count))
        elif cur_count > base_count:
            increased_diags.append((key, base_count, cur_count))

    # 基线存在但当前减少或消失
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
        print("\nOK: No new or increased Ruff diagnostics relative to baseline.")
    else:
        print("\nFAIL: New or increased Ruff diagnostics found. Fix them or update the baseline with justification.")

    return passed


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <current-report.json> <baseline.json>", file=sys.stderr)
        return 2

    current_path, baseline_path = sys.argv[1], sys.argv[2]
    current = load_diagnostics(current_path)
    baseline = load_baseline(baseline_path)

    passed = compare(current, baseline)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
