"""股票主数据种子服务 - 从 pytdx 拉取 A 股全市场股票列表并写入 instruments 表。

向量化处理：使用 pandas DataFrame 批量构建与去重，executemany 批量插入。
冲突处理：ON CONFLICT (symbol) DO UPDATE（upsert，用 pytdx 数据覆盖已有记录）。
NFKC 归一化：name 列写入前做 NFKC 归一化，将全角字母转为半角（如 Ａ股指数 → A股指数）。
拼音首字母：name 归一化后生成 pinyin_initials（advice.md 第六节），落库供拼音搜索使用。

提供：
- fetch_instruments_from_pytdx: 从 pytdx 拉取股票列表（DataFrame）
- seed_instruments_from_pytdx: 拉取并写入数据库
- transform_instruments_df: 将 pytdx DataFrame 转换为 instruments 表结构

用法：
    from app.services.instrument_seed import seed_instruments_from_pytdx

    # 同步执行（需在同步上下文中调用，如脚本或 CLI）
    count = seed_instruments_from_pytdx()

副作用：写入 instruments 表（UPSERT，冲突时更新 name/pinyin_initials/market/status）。
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import PytdxAdapter
from app.models.instrument import Instrument
from app.services.pinyin_util import compute_pinyin_initials

logger = logging.getLogger(__name__)

# 股票代码过滤：仅保留 6 位数字代码（排除指数、板块等非股票标的）
# SH: 6xxxxx（主板）、688xxx（科创板）
# SZ: 0xxxxx（主板）、002xxx（中小板）、3xxxxx（创业板）
# BJ: 8xxxxx、4xxxxx（北交所，pytdx 暂不支持，需其他数据源补充）
STOCK_CODE_PATTERN = r"^\d{6}$"


def _filter_stock_codes(df: pd.DataFrame) -> pd.DataFrame:
    """过滤非股票代码（指数、板块等），仅保留 6 位数字代码。

    Args:
        df: 含 'code' 列的 DataFrame

    Returns:
        过滤后的 DataFrame，仅保留 6 位数字代码行
    """
    mask = df["code"].str.match(STOCK_CODE_PATTERN, na=False)
    filtered = df[mask].copy()
    dropped = len(df) - len(filtered)
    if dropped > 0:
        logger.info("过滤非 6 位数字代码 %d 条，剩余 %d 条", dropped, len(filtered))
    return filtered


def transform_instruments_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    """将 pytdx 拉取的原始 DataFrame 转换为 instruments 表结构。

    转换步骤（向量化）：
    1. 过滤非 6 位数字代码
    2. 重命名列：code -> symbol
    3. 去重（按 symbol）
    4. 补充默认字段：status='active', listing_date=None
    5. name NFKC 归一化 + 生成 pinyin_initials

    Args:
        raw_df: pytdx 拉取的 DataFrame，列：code, name, market

    Returns:
        转换后的 DataFrame，列：symbol, name, pinyin_initials, market, status, listing_date
    """
    if raw_df.empty:
        return pd.DataFrame(
            columns=["symbol", "name", "pinyin_initials", "market", "status", "listing_date"]
        )

    # 过滤非股票代码
    df = _filter_stock_codes(raw_df)

    # 重命名列
    df = df.rename(columns={"code": "symbol"})

    # 去重（按 symbol，保留第一条）
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["symbol"], keep="first")
    after_dedup = len(df)
    if before_dedup != after_dedup:
        logger.info("symbol 去重：%d -> %d（删除 %d 条）", before_dedup, after_dedup, before_dedup - after_dedup)

    # 补充默认字段
    df["status"] = "active"
    df["listing_date"] = None  # pytdx get_security_list 不提供上市日期

    # NFKC 归一化：将全角字母转为半角（如 Ａ股指数 → A股指数，ＥＴＦｓ → ETFs）
    df["name"] = df["name"].apply(lambda x: unicodedata.normalize("NFKC", x))

    # 生成拼音首字母（advice.md 第六节：同步时落库，搜索时直接读字段，避免实时转拼音）
    df["pinyin_initials"] = df["name"].apply(compute_pinyin_initials)

    # 列顺序标准化
    df = df[["symbol", "name", "pinyin_initials", "market", "status", "listing_date"]]
    return df.reset_index(drop=True)


def fetch_instruments_from_pytdx(
    adapter: PytdxAdapter | None = None,
    market: str | None = None,
    max_count: int | None = None,
) -> pd.DataFrame:
    """从 pytdx 拉取股票列表并转换为 instruments 表结构。

    Args:
        adapter: 已连接的 PytdxAdapter，None 表示内部创建并连接
        market: 市场标识（SH/SZ），None 表示拉取全部 SH+SZ
        max_count: 每个市场最多拉取条数（自测用），None 表示全部

    Returns:
        转换后的 DataFrame，列：symbol, name, pinyin_initials, market, status, listing_date

    Raises:
        RuntimeError: pytdx 连接或拉取失败
    """
    if adapter is not None:
        raw_df = adapter.get_stock_list(market=market, max_count=max_count)
        return transform_instruments_df(raw_df)

    # 内部创建适配器
    with PytdxAdapter() as inner_adapter:
        raw_df = inner_adapter.get_stock_list(market=market, max_count=max_count)
        return transform_instruments_df(raw_df)


async def seed_instruments_from_pytdx(
    session: AsyncSession,
    market: str | None = None,
    max_count: int | None = None,
) -> int:
    """从 pytdx 拉取股票列表并写入 instruments 表。

    冲突处理：ON CONFLICT (symbol) DO UPDATE（upsert，用 pytdx 数据覆盖已有记录）。
    向量化：pandas 批量构建记录，executemany 批量插入。

    Args:
        session: 异步数据库会话
        market: 市场标识（SH/SZ），None 表示拉取全部 SH+SZ
        max_count: 每个市场最多拉取条数（自测用），None 表示全部

    Returns:
        新插入或更新的记录数

    Raises:
        RuntimeError: pytdx 连接或拉取失败
        Exception: 数据库写入失败（不吞没，由调用方处理）
    """
    logger.info("开始从 pytdx 拉取股票主数据：market=%s, max_count=%s", market, max_count)

    # 拉取并转换
    df = fetch_instruments_from_pytdx(market=market, max_count=max_count)

    if df.empty:
        logger.warning("pytdx 拉取股票列表为空，跳过写入")
        return 0

    logger.info("pytdx 拉取完成，共 %d 条股票记录，开始写入数据库", len(df))

    # 向量化构建插入记录
    records: list[dict[str, Any]] = df.to_dict(orient="records")

    # 分批插入（避免 PostgreSQL 参数数量限制，每批 1000 条）
    BATCH_SIZE = 1000
    total_inserted = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        stmt = pg_insert(Instrument).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "name": stmt.excluded.name,
                "pinyin_initials": stmt.excluded.pinyin_initials,
                "market": stmt.excluded.market,
                "status": stmt.excluded.status,
                "updated_at": func.now(),
            },
        )
        result = await session.execute(stmt)
        total_inserted += result.rowcount or 0

    await session.commit()

    logger.info("股票主数据写入完成：插入或更新 %d 条（总处理 %d 条）", total_inserted, len(records))
    return total_inserted


if __name__ == "__main__":
    # 自测入口：小批量验证（不写库表，仅拉取并转换）
    # 注意：需要网络访问 pytdx 服务器
    print("=== instrument_seed 自测（小批量，不写库）===")
    try:
        df = fetch_instruments_from_pytdx(market="SH", max_count=50)
        print(f"SH 市场前 50 条转换结果：{len(df)} 行")
        if not df.empty:
            print(df.head(5).to_string(index=False))
            print(f"\n列名：{list(df.columns)}")
            print(f"status 唯一值：{df['status'].unique()}")
            print(f"market 唯一值：{df['market'].unique()}")
    except RuntimeError as e:
        print(f"自测失败（网络或服务器问题）：{e}")
    print("=== 自测结束 ===")
