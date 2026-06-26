"""一次性回补 instruments.pinyin_initials - advice.md 第六节。

用法（在 trading-backend 容器内 /app 目录执行）：
    docker cp backend/scripts/backfill_pinyin_initials.py trading-backend:/app/backfill_pinyin_initials.py
    docker exec -w /app trading-backend python backfill_pinyin_initials.py

作用：
- 查询 instruments 全表 id + name
- 用 app.services.pinyin_util.compute_pinyin_initials 生成拼音首字母
- 分批 UPDATE 落库（默认 --reset=false 仅回补 NULL 行；--reset=true 全量重算）

副作用：UPDATE instruments.pinyin_initials（不改动其他字段）。生产库可直接运行。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Iterable

from sqlalchemy import create_engine, text

from app.services.pinyin_util import compute_pinyin_initials

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_pinyin")

BATCH_SIZE = 500


def _batch(iterable: Iterable, size: int):
    buf: list = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 instruments.pinyin_initials")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="全量重算（默认仅回补 pinyin_initials IS NULL 的行）",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL 环境变量未设置")
        return 2
    # alembic env.py 使用 psycopg3 同步驱动，这里保持一致
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    engine = create_engine(db_url, future=True)

    where_clause = "" if args.reset else "WHERE pinyin_initials IS NULL"
    count_sql = text(f"SELECT COUNT(*) FROM instruments {where_clause}")
    select_sql = text(f"SELECT id, name FROM instruments {where_clause}")

    with engine.connect() as conn:
        total_to_update = conn.execute(count_sql).scalar_one()
    logger.info("待回补行数：%d（reset=%s）", total_to_update, args.reset)
    if total_to_update == 0:
        logger.info("无需回补，退出")
        return 0

    update_sql = text("UPDATE instruments SET pinyin_initials = :pi WHERE id = :id")

    updated = 0
    skipped_empty = 0
    with engine.connect() as conn:
        result = conn.execute(select_sql)
        rows = result.fetchall()

    for batch in _batch(rows, args.batch_size):
        params = []
        for row in batch:
            inst_id, name = row[0], row[1]
            pi = compute_pinyin_initials(name)
            if pi is None:
                skipped_empty += 1
                continue
            params.append({"pi": pi, "id": inst_id})
        if not params:
            continue
        with engine.begin() as conn:
            conn.execute(update_sql, params)
        updated += len(params)
        logger.info("已回补 %d / %d", updated, total_to_update)

    logger.info("回补完成：updated=%d, skipped_empty=%d, total_to_update=%d", updated, skipped_empty, total_to_update)

    # 验证：抽样检查
    with engine.connect() as conn:
        sample = conn.execute(
            text("SELECT symbol, name, pinyin_initials FROM instruments ORDER BY name LIMIT 5")
        ).fetchall()
    logger.info("抽样验证（前 5 条）：")
    for sym, name, pi in sample:
        logger.info("  %s %s -> %s", sym, name, pi)

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
