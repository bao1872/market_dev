"""部署前数据库预检脚本。

用法（必须先设置 DATABASE_URL 或提供外部 env 文件）：
    DATABASE_URL=... python tools/pre_deploy_check.py
    python tools/pre_deploy_check.py --env-file /etc/market-dev/market.env

检查项：
1. 能连接到 PostgreSQL
2. 当前数据库名等于 bz_stock
3. alembic revision 等于 head
4. 必要表存在

预检失败时退出码非 0，且脚本不会创建数据库、拉取镜像或运行迁移。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

# 运行系统必须存在的核心表
REQUIRED_TABLES = [
    "alembic_version",
    "users",
    "instruments",
    "strategy_definitions",
    "strategy_versions",
    "strategy_runs",
    "scheduler_job_runs",
    "outbox",
    "message_deliveries",
    "notification_channels",
]


def _load_env_file(path: str) -> None:
    """按 KEY=VALUE 格式加载外部 env 文件到 os.environ。"""
    env_path = Path(path)
    if not env_path.exists():
        print(f"ERROR: env 文件不存在: {path}", file=sys.stderr)
        sys.exit(2)
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _parse_database_url(url: str) -> tuple[str, str, str, str]:
    """从 postgresql+psycopg://user:pass@host:port/dbname 解析连接参数。"""
    if not url:
        raise ValueError("DATABASE_URL 为空")
    # 移除驱动前缀
    url = url.replace("postgresql+psycopg://", "postgresql://")
    try:
        rest = url.split("://", 1)[1]
        creds, host_part = rest.split("@", 1)
        user, password = creds.split(":", 1)
        host_db = host_part.split("/", 1)
        host_port = host_db[0].rsplit(":", 1)
        host = host_port[0]
        port = host_port[1] if len(host_port) > 1 else "5432"
        dbname = host_db[1] if len(host_db) > 1 else ""
        return user, password, host, port, dbname
    except Exception as exc:
        raise ValueError(f"无法解析 DATABASE_URL: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="部署前数据库预检")
    parser.add_argument(
        "--env-file",
        default=None,
        help="外部 env 文件路径，例如 /etc/market-dev/market.env",
    )
    args = parser.parse_args()

    if args.env_file:
        _load_env_file(args.env_file)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL 未设置", file=sys.stderr)
        return 2

    # 解析连接参数
    try:
        user, password, host, port, dbname = _parse_database_url(database_url)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # 1. 连接数据库
    conn_str = f"host={host} port={port} dbname={dbname} user={user} password={password} connect_timeout=5"
    try:
        with psycopg.connect(conn_str) as conn:
            # 2. 检查数据库名
            cur_db = conn.execute("SELECT current_database()").fetchone()[0]
            if cur_db != "bz_stock":
                print(f"ERROR: 当前数据库名是 {cur_db}，必须是 bz_stock", file=sys.stderr)
                return 1
            print(f"OK: 数据库连接正常，current_database={cur_db}")

            # 3. 检查 alembic revision 是否为 head
            try:
                current_rev = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
            except psycopg.Error as exc:
                print(f"ERROR: 无法读取 alembic_version: {exc}", file=sys.stderr)
                return 1

            # 通过运行 alembic current 获取 head（本地 revision）
            import subprocess

            backend_dir = Path(__file__).resolve().parent.parent / "backend"
            try:
                result = subprocess.run(
                    ["alembic", "current"],
                    cwd=backend_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                    env={**os.environ, "DATABASE_URL": database_url},
                )
                head_marker = result.stdout.strip()
            except subprocess.CalledProcessError as exc:
                print(f"ERROR: alembic current 执行失败: {exc.stderr}", file=sys.stderr)
                return 1

            if current_rev not in head_marker or "head" not in head_marker:
                print(
                    f"ERROR: 数据库 alembic revision={current_rev} 不是当前 head: {head_marker}",
                    file=sys.stderr,
                )
                return 1
            print(f"OK: alembic revision={current_rev} 是当前 head")

            # 4. 检查必要表存在
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
            existing_tables = {row[0] for row in rows}
            missing = [t for t in REQUIRED_TABLES if t not in existing_tables]
            if missing:
                print(f"ERROR: 缺少必要表: {missing}", file=sys.stderr)
                return 1
            print(f"OK: 必要表全部存在 ({len(REQUIRED_TABLES)} 个)")

    except psycopg.OperationalError as exc:
        print(f"ERROR: 数据库连接失败: {exc}", file=sys.stderr)
        return 1

    print("PRE_DEPLOY_CHECK_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
