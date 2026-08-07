#!/usr/bin/env python3
"""生成 attempt-scoped 验证环境变量文件（attempt.env）。

减法版（2026-08-06）：
  - 只 inspect trading-postgres 拿 PG 凭据与共享网络（验证库跑在已有 PG 上）
  - 不再 inspect backend/frontend（不启动 verify-backend / verify-frontend 容器）
  - 不再输出 VERIFY_TEST_IMAGE / VERIFY_BACKEND_IMAGE / VERIFY_FRONTEND_IMAGE / host port
  - 本轮 verification 不连接 Redis（一次性审计结论：full-closure 仅连 PG），不输出 REDIS_URL
  - attempt.env 写入固定 runtime 路径（容器内只读挂载 /run/panji-verify/attempt.env）
  - 容器常驻 env 只持有稳定变量（APP_ENV/PANJI_SCHEDULER_ENABLED/TZ）；
    attempt-specific 变量全部来自本文件，由 verify_exec.py 动态注入每个 fresh process

attempt.env 内容（最小必要）：
  DATABASE_URL / MIGRATION_DATABASE_URL  → 精确验证库 bz_stock_verify_<SHA>
  TARGET_SHA                            → exact 40 位 SHA
  ATTEMPT_ID                            → 本次 attempt 标识（供 evidence 路径）
  JWT_SECRET                            → fresh 随机 secret（attempt scoped）
  APP_ENV / PANJI_SCHEDULER_ENABLED     → 稳定验证环境标志
  PANJI_VERIFY_PG_NETWORK               → 共享 PG 网络（供 fresh process 连接 trading-postgres）
"""

from __future__ import annotations

import argparse
import json
import secrets
import stat
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote


def _inspect(name: str) -> dict:
    result = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, check=True
    )
    payload = json.loads(result.stdout)
    if len(payload) != 1:
        raise RuntimeError(f"expected one docker object for {name}")
    return payload[0]


def _container_env(container: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in container.get("Config", {}).get("Env", []):
        key, separator, value = item.partition("=")
        if separator:
            values[key] = value
    return values


def prepare(target_sha: str, output: Path) -> Path:
    if len(target_sha) != 40 or any(ch not in "0123456789abcdef" for ch in target_sha):
        raise ValueError("target SHA must be complete lowercase hex")

    # 只 inspect trading-postgres：拿 PG 凭据与共享网络
    postgres = _inspect("trading-postgres")
    pg_env = _container_env(postgres)
    user = pg_env.get("POSTGRES_USER")
    password = pg_env.get("POSTGRES_PASSWORD")
    if not user or password is None:
        raise RuntimeError("trading-postgres is missing PostgreSQL credentials")

    pg_networks = set(postgres.get("NetworkSettings", {}).get("Networks", {}))
    if not pg_networks:
        raise RuntimeError("trading-postgres has no usable Docker network")

    # 验证库名严格匹配 bz_stock_verify_<40hex>（DS-110）
    db_name = f"bz_stock_verify_{target_sha}"
    database_url = (
        f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@trading-postgres:5432/{db_name}"
    )
    migration_database_url = database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg://", 1
    )

    # 固定 runtime 路径（容器内只读挂载 /run/panji-verify/:ro）
    runtime_dir = output.parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "RUNTIME_SHA").write_text(target_sha, encoding="ascii")

    attempt_id = uuid.uuid4().hex
    lines = {
        # attempt-specific（由 verify_exec.py 动态注入，不进 container create env）
        "DATABASE_URL": database_url,
        "MIGRATION_DATABASE_URL": migration_database_url,
        "TARGET_SHA": target_sha,
        "ATTEMPT_ID": attempt_id,
        "JWT_SECRET": secrets.token_urlsafe(48),
        # 稳定标志（也写入 attempt.env 以便 fresh process 一致性；容器常驻 env 同样持有）
        "APP_ENV": "verification",
        "PANJI_SCHEDULER_ENABLED": "false",
        # fresh process 连接 trading-postgres 所需网络（容器内通过 docker network 已 join）
        "PANJI_VERIFY_PG_NETWORK": min(pg_networks),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{key}={value}\n" for key, value in lines.items()),
        encoding="utf-8",
    )
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = prepare(args.target_sha, args.output)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
