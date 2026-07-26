#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "AGENTS.md",
    "rules/README.md",
    "rules/50-git-development-flow.md",
    "rules/70-trae-cn.md",
    "rules/80-auto-deployment-data-safety.md",
    "rules/85-server-directory-boundaries.md",
    "maps/MANIFEST.md",
    "maps/current/INDEX.md",
    "maps/current/09-development-deployment-workflow.md",
    "maps/code/deployment-runtime-map.md",
    "maps/runbooks/AUTO-DEPLOY-DEV.md",
    "maps/work/IMPLEMENTATION-PLAN-AUTO-DEPLOY.md",
]

LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    missing = [x for x in REQUIRED if not (ROOT / x).is_file()]
    if missing:
        print("[FAIL] missing:\n" + "\n".join(missing))
        return 1

    broken: list[str] = []
    files = [ROOT / "AGENTS.md", *ROOT.glob("rules/**/*.md"), *ROOT.glob("maps/**/*.md")]
    for md in files:
        text = md.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (md.parent / target).resolve().exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")

    if broken:
        print("[FAIL] broken links:\n" + "\n".join(broken))
        return 1

    print("[PASS] knowledge system")
    return 0


if __name__ == "__main__":
    sys.exit(main())
