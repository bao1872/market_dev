"""只读数据访问层 — 从研究矩阵和 bars_daily 读取数据。

所有 SQL 运行在只读事务中，设置 statement_timeout。
不使用生产公网直连，复用实验主机 127.0.0.1:15432 安全隧道。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("regime_discovery.data_access")

# 研究矩阵因果白名单列（仅 causal + confirmed_delay，排除 hindsight/label/amount）
FEATURE_MATRIX_COLUMNS: list[str] = [
    # metadata
    "instrument_id",
    "symbol",
    "trade_date",
    # causal (16)
    "causal_atr",
    "causal_bb_percent_b",
    "causal_bb_bandwidth_pct",
    "causal_sqzmom_val",
    "causal_sqzmom_delta_1",
    "causal_volume_ratio_20",
    "causal_volume_percentile_120",
    "causal_active_swing_dir",
    "causal_active_swing_high",
    "causal_active_swing_low",
    "causal_developing_swing_dir",
    "causal_developing_swing_high",
    "causal_developing_swing_low",
    "causal_dsa_confirmed_segment",
    "causal_dsa_confirmed_direction",
    "causal_dsa_confirmed_age_bars",
    # confirmed_delay (4)
    "confirmed_delay_confirmed_swing_high",
    "confirmed_delay_confirmed_swing_low",
    "confirmed_delay_bars_since_confirmed_swing_high",
    "confirmed_delay_bars_since_confirmed_swing_low",
]

# 禁止进入聚类 X 的列前缀
FORBIDDEN_PREFIXES = ("hindsight_", "label_", "amount")

# bars_daily 只读列
BARS_DAILY_COLUMNS = ["instrument_id", "trade_date", "close"]

STATEMENT_TIMEOUT_SECONDS = 120

# fetch_close_prices 分批查询的 instrument_id 批大小，避免单条 SQL 超过 statement_timeout
CLOSE_PRICE_BATCH_SIZE = 200


def _resolve_db_url() -> str:
    """解析只读 DB URL，优先环境变量，其次 .env.lab。"""
    url = os.environ.get("PROD_READONLY_DATABASE_URL")
    if url:
        return url
    env_lab = Path("/home/ubuntu/market_dev/.env.lab")
    if env_lab.exists():
        for line in env_lab.read_text().splitlines():
            if line.startswith("PROD_READONLY_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "无法解析 PROD_READONLY_DATABASE_URL。"
        "请设置环境变量或确保 .env.lab 存在。"
    )


def create_readonly_engine() -> Engine:
    """创建只读 SQLAlchemy engine（同步）。"""
    url = _resolve_db_url()
    # 确保使用 psycopg 同步驱动
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    logger.info("创建只读 engine: %s", engine.url)
    return engine


def get_session(engine: Engine) -> Session:
    """创建只读 session，设置 READ ONLY + statement_timeout。"""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    # 设置 statement_timeout
    session.execute(text(f"SET statement_timeout = '{STATEMENT_TIMEOUT_SECONDS}s'"))
    session.execute(text("SET default_transaction_read_only = 'on'"))
    session.commit()
    return session


def get_data_as_of(session: Session) -> date:
    """查询研究矩阵最大 trade_date。"""
    result = session.execute(
        text("SELECT MAX(trade_date) FROM research_feature_matrix_rows")
    ).scalar()
    if result is None:
        raise RuntimeError("research_feature_matrix_rows 表为空")
    return result


def get_data_range(session: Session) -> tuple[date, date]:
    """查询研究矩阵最小和最大 trade_date。"""
    result = session.execute(
        text(
            "SELECT MIN(trade_date), MAX(trade_date) "
            "FROM research_feature_matrix_rows"
        )
    ).one()
    return result[0], result[1]


def get_total_row_count(
    session: Session, start: date | None = None, end: date | None = None
) -> int:
    """查询研究矩阵行数。"""
    sql = "SELECT count(*) FROM research_feature_matrix_rows"
    params: dict[str, date] = {}
    if start and end:
        sql += " WHERE trade_date BETWEEN :start AND :end"
        params = {"start": start, "end": end}
    elif start:
        sql += " WHERE trade_date >= :start"
        params = {"start": start}
    elif end:
        sql += " WHERE trade_date <= :end"
        params = {"end": end}
    return int(session.execute(text(sql), params).scalar() or 0)


def fetch_close_prices(
    session: Session,
    instrument_ids: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """从 bars_daily 只读 join close 价格。

    当 instrument_ids 超过 CLOSE_PRICE_BATCH_SIZE 时，分批查询以避免
    单条 SQL 在 statement_timeout 限制下超时。

    Returns:
        DataFrame with columns: instrument_id, trade_date, close
    """
    base_conditions: list[str] = []
    base_params: dict[str, object] = {}
    if start:
        base_conditions.append("trade_date >= :start")
        base_params["start"] = start
    if end:
        base_conditions.append("trade_date <= :end")
        base_params["end"] = end

    if instrument_ids and len(instrument_ids) > CLOSE_PRICE_BATCH_SIZE:
        frames: list[pd.DataFrame] = []
        for i in range(0, len(instrument_ids), CLOSE_PRICE_BATCH_SIZE):
            batch = instrument_ids[i : i + CLOSE_PRICE_BATCH_SIZE]
            conditions = [*base_conditions, "instrument_id = ANY(:inst_ids)"]
            params = {**base_params, "inst_ids": batch}
            sql = (
                "SELECT instrument_id, trade_date, close FROM bars_daily"
                " WHERE " + " AND ".join(conditions)
                + " ORDER BY instrument_id, trade_date"
            )
            frames.append(
                pd.read_sql(text(sql), session.connection(), params=params)
            )
        if not frames:
            return pd.DataFrame(columns=BARS_DAILY_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    conditions = list(base_conditions)
    params = dict(base_params)
    if instrument_ids:
        conditions.append("instrument_id = ANY(:inst_ids)")
        params["inst_ids"] = instrument_ids
    sql = "SELECT instrument_id, trade_date, close FROM bars_daily"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY instrument_id, trade_date"
    return pd.read_sql(text(sql), session.connection(), params=params)


def stratified_sample(
    session: Session,
    sample_rows: int = 150000,
    seed: int = 42,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """按月份和股票分层抽样。

    按月份和股票分组，每组等比例抽样，最多 sample_rows 行。
    固定 seed 保证可复现。
    """
    conditions: list[str] = []
    params: dict[str, object] = {}
    if start:
        conditions.append("trade_date >= :start")
        params["start"] = start
    if end:
        conditions.append("trade_date <= :end")
        params["end"] = end
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # 先查所有行的 (instrument_id, trade_date) 用于分层
    count_sql = f"SELECT count(*) FROM research_feature_matrix_rows{where_clause}"
    total = int(session.execute(text(count_sql), params).scalar() or 0)
    if total == 0:
        raise RuntimeError("研究矩阵无数据")

    actual_sample = min(sample_rows, total)
    if actual_sample >= total:
        # 全量读取
        sql = (
            f"SELECT {', '.join(FEATURE_MATRIX_COLUMNS)} "
            f"FROM research_feature_matrix_rows{where_clause} "
            "ORDER BY instrument_id, trade_date"
        )
        df = pd.read_sql(text(sql), session.connection(), params=params)
        return df.astype(_dtype_map())

    # 分层抽样：按月份分组，每组按比例抽样
    rng = np.random.default_rng(seed)
    month_sql = (
        f"SELECT DISTINCT to_char(trade_date, 'YYYY-MM') AS month "
        f"FROM research_feature_matrix_rows{where_clause} ORDER BY month"
    )
    months = [r[0] for r in session.execute(text(month_sql), params)]

    frames: list[pd.DataFrame] = []
    remaining = actual_sample
    for i, month in enumerate(months):
        if remaining <= 0:
            break
        # 最后一个月取剩余全部
        if i == len(months) - 1:
            month_sample = remaining
        else:
            month_sample = max(1, remaining // (len(months) - i))
        remaining -= month_sample

        m_conditions = list(conditions)
        m_params = dict(params)
        m_conditions.append("to_char(trade_date, 'YYYY-MM') = :month")
        m_params["month"] = month
        m_where = " WHERE " + " AND ".join(m_conditions)

        # 查该月行数
        m_count = int(
            session.execute(
                text(f"SELECT count(*) FROM research_feature_matrix_rows{m_where}"),
                m_params,
            ).scalar() or 0
        )
        month_sample = min(month_sample, m_count)

        # 随机抽样
        sql = (
            f"SELECT {', '.join(FEATURE_MATRIX_COLUMNS)} "
            f"FROM research_feature_matrix_rows{m_where} "
            "ORDER BY random() "
            f"LIMIT {month_sample}"
        )
        # 用 seed 控制随机性：先查所有 instrument_id，再抽样
        inst_sql = (
            f"SELECT DISTINCT instrument_id FROM research_feature_matrix_rows{m_where}"
        )
        inst_ids = [r[0] for r in session.execute(text(inst_sql), m_params)]
        if len(inst_ids) > 1:
            sampled_inst = rng.choice(
                inst_ids, size=min(len(inst_ids), max(1, month_sample // 20)),
                replace=False,
            )
            m_conditions.append("instrument_id = ANY(:sampled_inst)")
            m_params["sampled_inst"] = list(sampled_inst)
            m_where = " WHERE " + " AND ".join(m_conditions)

        sql = (
            f"SELECT {', '.join(FEATURE_MATRIX_COLUMNS)} "
            f"FROM research_feature_matrix_rows{m_where} "
            f"ORDER BY instrument_id, trade_date LIMIT {month_sample}"
        )
        df_m = pd.read_sql(text(sql), session.connection(), params=m_params)
        frames.append(df_m)

    df = pd.concat(frames, ignore_index=True)
    # 如果超过 sample_rows，截断
    if len(df) > actual_sample:
        df = df.iloc[:actual_sample]
    return df.astype(_dtype_map())


def iter_chunks(
    session: Session,
    chunk_size: int = 25000,
    start: date | None = None,
    end: date | None = None,
) -> Iterator[pd.DataFrame]:
    """分块迭代全量研究矩阵数据。"""
    conditions: list[str] = []
    params: dict[str, object] = {}
    if start:
        conditions.append("trade_date >= :start")
        params["start"] = start
    if end:
        conditions.append("trade_date <= :end")
        params["end"] = end
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    offset = 0
    while True:
        sql = (
            f"SELECT {', '.join(FEATURE_MATRIX_COLUMNS)} "
            f"FROM research_feature_matrix_rows{where_clause} "
            "ORDER BY instrument_id, trade_date "
            f"LIMIT {chunk_size} OFFSET {offset}"
        )
        df = pd.read_sql(text(sql), session.connection(), params=params)
        if df.empty:
            break
        yield df.astype(_dtype_map())
        offset += chunk_size


def get_all_matrix_rows(
    session: Session,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """读取全量研究矩阵行（不分块），用于 Phase E 全量 assignment。

    确保横截面 rank 基于完整市场横截面计算，而非 chunk 子集。
    全量读取可能超过默认 statement_timeout，因此临时提升至 600s。
    不在 SQL 中排序（build_features 会排序，避免重复排序）。
    使用 chunksize 流式读取 + 逐 chunk 转 float32，降低峰值 RSS。

    注意：chunksize 只用于控制读取时的内存峰值，不影响横截面 rank
    正确性 — 所有 chunk 最终合并为一个完整 DataFrame，rank 在完整
    横截面上计算。

    Args:
        session: 只读 SQLAlchemy session
        start: 可选起始日期
        end: 可选结束日期

    Returns:
        全量研究矩阵 DataFrame（未排序，由调用方排序）
    """
    conditions: list[str] = []
    params: dict[str, object] = {}
    if start:
        conditions.append("trade_date >= :start")
        params["start"] = start
    if end:
        conditions.append("trade_date <= :end")
        params["end"] = end
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # 全量读取需要更长的 statement_timeout（621k 行 × 23 列）
    session.execute(text("SET LOCAL statement_timeout = '600s'"))
    sql = (
        f"SELECT {', '.join(FEATURE_MATRIX_COLUMNS)} "
        f"FROM research_feature_matrix_rows{where_clause}"
    )
    # 使用 chunksize 流式读取，逐 chunk 转 float32，避免 float64 全量缓冲
    numeric_cols = [
        c for c in FEATURE_MATRIX_COLUMNS
        if c not in ("instrument_id", "symbol", "trade_date")
    ]
    chunks: list[pd.DataFrame] = []
    for chunk in pd.read_sql(
        text(sql), session.connection(), params=params, chunksize=50000
    ):
        for col in numeric_cols:
            chunk[col] = chunk[col].astype(np.float32)
        chunks.append(chunk)
    df = pd.concat(chunks, ignore_index=True)
    del chunks
    return df


def get_all_instrument_ids(
    session: Session,
    start: date | None = None,
    end: date | None = None,
) -> list[str]:
    """返回研究矩阵中所有 distinct instrument_id。

    Args:
        session: 只读 SQLAlchemy session
        start: 可选起始日期
        end: 可选结束日期

    Returns:
        instrument_id 字符串列表
    """
    conditions: list[str] = []
    params: dict[str, object] = {}
    if start:
        conditions.append("trade_date >= :start")
        params["start"] = start
    if end:
        conditions.append("trade_date <= :end")
        params["end"] = end
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = text(
        f"SELECT DISTINCT instrument_id FROM research_feature_matrix_rows{where_clause}"
    )
    return [str(r[0]) for r in session.execute(sql, params)]


def get_month_instrument_coverage(
    session: Session,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """按月和股票的覆盖率统计。"""
    conditions: list[str] = []
    params: dict[str, object] = {}
    if start:
        conditions.append("trade_date >= :start")
        params["start"] = start
    if end:
        conditions.append("trade_date <= :end")
        params["end"] = end
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = text(
        f"""
        SELECT to_char(trade_date, 'YYYY-MM') AS month,
               count(DISTINCT instrument_id) AS instrument_count,
               count(*) AS row_count
        FROM research_feature_matrix_rows{where_clause}
        GROUP BY to_char(trade_date, 'YYYY-MM')
        ORDER BY month
        """
    )
    return pd.read_sql(sql, session.connection(), params=params)


def _dtype_map() -> dict[str, str]:
    """返回研究矩阵列的 dtype 映射，float 列使用 float32 节省内存。"""
    dtypes: dict[str, str] = {}
    for col in FEATURE_MATRIX_COLUMNS:
        if col in ("instrument_id", "symbol", "trade_date"):
            if col == "trade_date":
                dtypes[col] = "datetime64[ns]"
            elif col == "symbol":
                dtypes[col] = "string"
            # instrument_id 保持 object (UUID)
        else:
            dtypes[col] = "float32"
    return dtypes
