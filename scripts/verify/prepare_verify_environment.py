#!/usr/bin/env python3
"""Create one attempt-scoped verification env without printing secrets."""

from __future__ import annotations

import argparse
import json
import secrets
import stat
import subprocess
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
    postgres = _inspect("trading-postgres")
    backend = _inspect("trading-backend")
    frontend = _inspect("trading-frontend")
    pg_env = _container_env(postgres)
    user = pg_env.get("POSTGRES_USER")
    password = pg_env.get("POSTGRES_PASSWORD")
    if not user or password is None:
        raise RuntimeError("trading-postgres is missing PostgreSQL credentials")
    pg_networks = set(postgres.get("NetworkSettings", {}).get("Networks", {}))
    backend_networks = set(backend.get("NetworkSettings", {}).get("Networks", {}))
    networks = sorted(pg_networks & backend_networks)
    if not networks:
        raise RuntimeError("no shared Docker network for PostgreSQL verification")

    db_name = f"bz_stock_verify_{target_sha}"
    database_url = (
        f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@trading-postgres:5432/{db_name}"
    )
    migration_database_url = database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg://", 1
    )
    runtime_dir = output.parent / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "RUNTIME_SHA").write_text(target_sha, encoding="ascii")
    lines = {
        "DATABASE_URL": database_url,
        "MIGRATION_DATABASE_URL": migration_database_url,
        "REDIS_URL": "redis://verify-redis:6379/0",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "APP_ENV": "verification",
        "PANJI_SCHEDULER_ENABLED": "false",
        "CONFIG_FILE": "/app/config.production.py",
        "VERIFY_CODE_DIR": "/root/web_dev_verify",
        "VERIFY_RUNTIME_DIR": str(runtime_dir),
        "VERIFY_PG_NETWORK": networks[0],
        "VERIFY_BACKEND_IMAGE": backend["Config"]["Image"],
        "VERIFY_FRONTEND_IMAGE": frontend["Config"]["Image"],
        "VERIFY_TEST_IMAGE": f"panji-verify-test:{target_sha}",
        "VERIFY_BACKEND_HOST_PORT": "18000",
        "VERIFY_FRONTEND_HOST_PORT": "18080",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(f"{key}={value}\n" for key, value in lines.items()), encoding="utf-8")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
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
