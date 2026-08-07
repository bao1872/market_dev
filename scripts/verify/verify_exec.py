#!/usr/bin/env python3
"""verify_exec — attempt env 注入封装（唯一 gate wrapper）。

为什么不是 `env $(cat attempt.env) <cmd>`：
    shell word splitting 对带有空格/引号/特殊字符的 env value 处理不可靠，
    可能把单个 value 拆成多个 argv 或破坏引号。这里用 Python 解析 env 文件，
    再以 env= 参数传给 subprocess.run，保证每个 value 作为完整字符串注入。

职责（仅三件事，不做进程注册 / history / 状态机）：
    1. 读取固定 runtime 路径下的 attempt.env（容器内只读挂载 /run/panji-verify/attempt.env）
    2. 按第一个 '=' 拆出 key/value
    3. subprocess.run(command, env=env)

容器常驻 env 只持有稳定变量（APP_ENV/PANJI_SCHEDULER_ENABLED/TZ）；
attempt-specific 变量（DATABASE_URL/REDIS_URL/JWT_SECRET/TARGET_SHA/...）
全部来自 attempt.env，由本封装动态注入每个 fresh process，
避免 container create env 携带 attempt secrets 导致跨 attempt 污染。

Redis 说明（2026-08-06 一次性审计结论）：
    full-closure 验证执行路径（alembic/pytest/seed/e2e）完全不依赖 Redis，
    只连 PostgreSQL。本轮 verification 不连接 Redis，attempt.env 不含 REDIS_URL。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 固定 runtime 控制路径：容器内只读挂载 /run/panji-verify/:ro
DEFAULT_ATTEMPT_ENV = "/run/panji-verify/attempt.env"


def load_attempt_env(path: str) -> dict[str, str]:
    """按第一个 '=' 拆 key/value。

    只解析简单 KEY=VALUE 行；跳过空行与 '#' 注释；
    若 value 为空也允许（KEY=）。不展开变量、不做引号处理。
    """
    env: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq <= 0:
            # 没有 '=' 或行首即为 '='：跳过，避免误注入
            continue
        key = line[:eq].strip()
        value = line[eq + 1 :]
        if not key:
            continue
        env[key] = value
    return env


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "usage: verify_exec.py <command...>\n"
            "  reads attempt.env (override via VERIFY_ATTEMPT_ENV) and injects into command env\n"
        )
        return 2

    env_path = os.environ.get("VERIFY_ATTEMPT_ENV", DEFAULT_ATTEMPT_ENV)
    if not Path(env_path).is_file():
        sys.stderr.write(f"verify_exec: attempt.env not found at {env_path}\n")
        return 3

    attempt_env = load_attempt_env(env_path)

    # 合并到当前进程 env（容器稳定变量 + attempt 变量；attempt 变量覆盖同名稳定变量）
    merged = dict(os.environ)
    merged.update(attempt_env)

    command = argv[1:]
    result = subprocess.run(command, env=merged, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
