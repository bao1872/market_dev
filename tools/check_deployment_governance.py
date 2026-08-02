#!/usr/bin/env python3
"""部署治理防回潮检查（Standalone）。

用途：
    验证有效治理文档（AGENTS.md / rules/ / docs/prd/ / docs/maps/ / docs/runbooks/）
    当前**不描述**与盘迹开发阶段无关的发布/部署流程，并确认 Live Mount 开发部署
    为唯一当前部署模式。

不扫描：
    - docs/changes/（历史 Change，允许保留历史流程，仅作历史记录）
    - .github/workflows/*.yml（历史遗留工作流文件，不视为当前操作指令）
    - backend/ frontend/ scripts/（业务/运维代码，不在本检查范围）

退出码：0 通过；1 发现回潮（forbidden 术语出现在有效治理文档）。

用法：
    python tools/check_deployment_governance.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()

# 有效治理文档范围（不含历史 Change 与 CI workflow）
SCAN_DIRS = [
    ROOT / "AGENTS.md",
    ROOT / "rules",
    ROOT / "docs" / "prd",
    ROOT / "docs" / "maps",
    ROOT / "docs" / "runbooks",
]

# 当前无关、禁止在有效治理文档中描述/保留（含改名为 deferred 后保留）的流程术语。
# 每个条目为 (术语, 行内豁免关键词) —— 若术语出现且不在豁免语境中，则判违规。
#
# 豁免分两层：
#   1) 行内豁免：该行本身标注为历史/已废止/禁止清单条目；
#   2) 块级豁免：整段处于"禁止/已废止清单"标题下（见 FORBIDDEN_LIST_HEADINGS）。
#
# 通用行内豁免：任何标注该术语为历史或已禁止的行。
COMMON_EXEMPTIONS = [
    "历史",
    "superseded",
    "已废止",
    "已废弃",
    "不作为当前",
    "禁止",
    "不得",
    "废止",
    "historically",
]

FORBIDDEN_TERMS = [
    ("Release Gate", []),
    ("release-gate", []),
    ("GHCR", ["blocked_registry_auth"]),
    ("ghcr.io", []),
    # Registry 需限定为"镜像仓库"语义；Canonical Registry / ColumnRegistry /
    # ReviewMetricComponentRegistry 等业务概念与部署无关，通过行内豁免排除。
    (
        "Registry",
        [
            "registry_prefix",
            "PANJI_REGISTRY_PREFIX",
            "Canonical",
            "ColumnRegistry",
            "ComponentRegistry",
            "MetricRegistry",
            "因子",
            "指标",
        ],
    ),
    ("Release Manifest", []),
    ("release manifest", []),
    ("immutable image release", []),
    ("formal release candidate", []),
    ("formal release", []),
    ("只 pull 不 build", []),
    ("pull-only", []),
    ("Fast CI 作为部署强制门禁", []),
    ("CI Gate 作为部署", []),
    ("CI Gate = success", []),
    ("delivery phase", []),
    # 多阶段 仅在"交付/发布/部署"语义下违规；竞价 lifecycle 多阶段等业务概念豁免。
    ("多阶段 delivery", []),
    ("多阶段交付", []),
    ("多阶段发布", []),
    ("未来正式发布", []),
    ("自动部署已启用", []),
    ("自动部署已经启用", []),
    ("development 阶段状态机", []),
    ("formal_release 阶段", []),
    ("runtime 阶段状态机", []),
]

# 块级豁免：以下标题（或其变体）开始的小节，允许成段列举被禁流程名称，
# 因为这些小节的存在目的就是"声明这些流程已被删除/禁止"。
FORBIDDEN_LIST_HEADINGS = [
    "禁止",
    "已废止",
    "已废弃",
    "废止",
    "不属于当前",
    "不作为当前",
    "本轮删除",
    "已删除",
    "Deprecated",
    "Forbidden",
]

# 必须存在于有效治理文档中的 Live Mount 合同关键词（至少出现在 rules/80 或 runbook）。
REQUIRED_LIVE_MOUNT_SIGNALS = [
    "/opt/panji-live",
    "Live Mount",
    "RUNTIME_SHA",
]


def collect_scan_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCAN_DIRS:
        if entry.is_file():
            files.append(entry)
        elif entry.is_dir():
            for p in entry.rglob("*.md"):
                # 排除历史 Change 目录
                if "changes" in p.parts:
                    continue
                files.append(p)
    return files


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


class Violation:
    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def build_exempt_block_lines(lines: list[str]) -> set[int]:
    """返回处于"禁止/已废止清单"小节内的行号集合（1-based）。

    规则：遇到标题行时判断其文本是否命中 FORBIDDEN_LIST_HEADINGS；命中则该小节
    （直到出现同级或更高级标题前）的所有行进入豁免集合。
    """
    exempt: set[int] = set()
    active_level: int | None = None
    for i, line in enumerate(lines, start=1):
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2)
            if active_level is not None and level <= active_level:
                active_level = None
            if any(kw in title for kw in FORBIDDEN_LIST_HEADINGS):
                active_level = level
                exempt.add(i)
                continue
        if active_level is not None:
            exempt.add(i)
    return exempt


def check_no_forbidden_terms() -> list[Violation]:
    violations: list[Violation] = []
    files = collect_scan_files()
    for path in files:
        text = read_text(path)
        lines = text.splitlines()
        rel = path.relative_to(ROOT)
        exempt_lines = build_exempt_block_lines(lines)
        for term, extra_exemptions in FORBIDDEN_TERMS:
            exemptions = COMMON_EXEMPTIONS + extra_exemptions
            idx = text.find(term)
            while idx != -1:
                line_no = text[:idx].count("\n") + 1
                line = lines[line_no - 1] if line_no <= len(lines) else ""
                # 块级豁免：位于"禁止/已废止清单"小节内
                # 行内豁免：该行明确标注为历史/已废止/禁止项
                if line_no not in exempt_lines and not any(ex in line for ex in exemptions):
                    violations.append(
                        Violation(
                            "deployment-regression",
                            f"{rel}:{line_no} 出现当前无关流程术语 '{term}': {line.strip()[:120]}",
                        )
                    )
                idx = text.find(term, idx + 1)
    return violations


def check_live_mount_contract_present() -> list[Violation]:
    """确认 Live Mount 合同信号至少出现在 rules/80 或 runbook 中。"""
    violations: list[Violation] = []
    # 在 rules/80-deployment-data-safety.md 与 docs/runbooks/development-deployment.md 中查找
    targets = [
        ROOT / "rules" / "80-deployment-data-safety.md",
        ROOT / "docs" / "runbooks" / "development-deployment.md",
    ]
    found = {sig: False for sig in REQUIRED_LIVE_MOUNT_SIGNALS}
    for path in targets:
        text = read_text(path)
        for sig in REQUIRED_LIVE_MOUNT_SIGNALS:
            if sig in text:
                found[sig] = True
    missing = [sig for sig, ok in found.items() if not ok]
    if missing:
        violations.append(
            Violation(
                "live-mount-contract-missing",
                f"Live Mount 合同信号缺失: {missing}（须在 rules/80 或 development-deployment.md 中体现）",
            )
        )
    return violations


def check_current_deploy_mode_unique() -> list[Violation]:
    """确认有效治理文档声明 Live Mount 为唯一当前部署模式（非镜像构建为常态）。"""
    violations: list[Violation] = []
    text = read_text(ROOT / "rules" / "80-deployment-data-safety.md")
    if "唯一当前部署模式" not in text and "唯一部署模式" not in text and "唯一模式" not in text:
        # 退而求其次：要求存在 "Live Mount 开发部署" 与 "普通变更走 Live Mount"
        if "Live Mount 开发部署" not in text:
            violations.append(
                Violation(
                    "deploy-mode-not-unique",
                    "rules/80 未声明 Live Mount 为唯一当前部署模式",
                )
            )
    return violations


def main() -> int:
    violations: list[Violation] = []
    violations.extend(check_no_forbidden_terms())
    violations.extend(check_live_mount_contract_present())
    violations.extend(check_current_deploy_mode_unique())

    print("=" * 60)
    print("部署治理防回潮检查（check_deployment_governance.py）")
    print("=" * 60)

    if violations:
        print(f"\n发现 {len(violations)} 个违规：\n")
        for v in violations:
            print(f"  {v}")
        print("\n" + "=" * 60)
        print(f"结果: FAIL（{len(violations)} 个违规）")
        print("=" * 60)
        return 1

    print("\n部署治理检查通过：")
    print("  - 有效治理文档不含 Release Gate / GHCR / Registry / 多阶段发布等无关流程")
    print("  - Live Mount 开发部署为唯一当前部署模式")
    print("=" * 60)
    print("结果: PASS")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
