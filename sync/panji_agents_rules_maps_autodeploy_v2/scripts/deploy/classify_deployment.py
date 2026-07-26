#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    changed = [
        x for x in git("diff", "--name-only", args.base, args.target).splitlines()
        if x
    ]

    blocked_markers = (
        "backend/alembic/versions/",
        "Dockerfile",
        "docker-compose",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "nginx.conf",
    )
    blocked_files = [
        p for p in changed
        if any(marker in p for marker in blocked_markers)
    ]

    runtime = [
        p for p in changed
        if not (
            p == "AGENTS.md"
            or p.startswith("rules/")
            or p.startswith("maps/")
            or p.startswith("docs/")
            or p.startswith("backend/tests/")
            or p.startswith("frontend/e2e/")
            or p.startswith("scripts/contract-tests/")
            or p.startswith(".github/")
        )
    ]

    front = any(p.startswith("frontend/") for p in runtime)
    back = any(p.startswith("backend/app/") for p in runtime)

    if blocked_files:
        mode = "blocked"
    elif front and back:
        mode = "combined_live"
    elif front:
        mode = "frontend_live"
    elif back:
        mode = "python_live"
    elif runtime:
        mode = "blocked"
        blocked_files = runtime
    else:
        mode = "none"

    result = {
        "base": args.base,
        "target": args.target,
        "changed_files": changed,
        "runtime_files": runtime,
        "mode": mode,
        "blocked_files": blocked_files,
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
