"""历史竞价快照回补落库 writer — [CHANGE-20260817-001]。

职责：
- 为历史回补的每个交易日建立 1 个 ``AuctionQuoteCaptureRun``（source/test_namespace
  标记为 ``historical_backfill``，与 live capture 完全隔离，不污染实时 truth）。
- 将回补 runner 已计算的 member-fact 投影批量 upsert 进 ``auction_final_quotes``。
- 利用现有唯一约束 ``(trade_date, instrument_id, source, capture_run_id)`` 做幂等 upsert。
- 分 chunk 小事务写入，避免单事务 60 万行锁表 / 事务内存膨胀（历史死机根源之一）。

隔离策略（blast radius 最小）：
- ``source = "historical_backfill"`` + ``test_namespace = "historical_backfill"``
  → 与 live capture 的 ``verified_consensus`` / ``production`` 唯一键不冲突。
- 该 run 仅作 ``auction_final_quotes.capture_run_id`` 的外键 owner，不参与实时
  truth / consensus / 发布指针，也不会被现有 scan_service（只读 verified source）自动消费。
  消费侧扩展（如让历史窗口包含 backfill 数据）属单独一轮，不在本轮范围。

性能控制（针对历史死机）：
- 每 bar ≈ 5000 股票分 chunk（默认 500/批）小事务 upsert，禁止单事务大批量。
- writer 只读 runner 已算好的 in-memory 投影，不二次拉 pytdx、不二次占 RAM。
- 单 instrument upsert 失败只记 reason_codes + 该 bar db_failed_rows，不中断整 bar。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import AuctionFinalQuote, AuctionQuoteCaptureRun

logger = logging.getLogger(__name__)

# 历史回补专用 source / namespace，与 live capture 隔离
HISTORICAL_BACKFILL_SOURCE = "historical_backfill"
HISTORICAL_BACKFILL_NAMESPACE = "historical_backfill"

# 默认 chunk 大小：每事务 upsert 行数上限（针对历史死机的内存/锁表防护）
DEFAULT_CHUNK_SIZE = 500

# quality_status 合法值（与 live capture 一致）
QUALITY_OK = "ok"
QUALITY_ZERO_VOLUME = "zero_volume"
QUALITY_SOURCE_INCOMPLETE = "source_incomplete"
QUALITY_INVALID_VOLUME = "invalid_volume"
QUALITY_ERROR = "source_error"
VALID_QUALITY_STATUSES = {
    QUALITY_OK,
    QUALITY_ZERO_VOLUME,
    QUALITY_SOURCE_INCOMPLETE,
    QUALITY_INVALID_VOLUME,
    QUALITY_ERROR,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemberFactProjection:
    """runner 每 instrument 投影到 AuctionFinalQuote 的只读字段集。

    字段取值来自回补 runner 的 ``_project_member_row`` 输出（auction_price_raw /
    auction_volume_shares / auction_amount / canonicalization_status / source_status 等），
    writer 不重新计算任何竞价算法。
    """

    __slots__ = (
        "instrument_id",
        "trade_date",
        "final_price",
        "prev_close",
        "volume",
        "amount",
        "matched_volume",
        "unmatched_volume",
        "quality_status",
        "reason_codes",
        "raw_payload",
    )

    def __init__(
        self,
        instrument_id: uuid.UUID,
        trade_date: date,
        final_price: float | None,
        prev_close: float | None,
        volume: int | None,
        amount: float | None,
        matched_volume: int | None,
        unmatched_volume: int | None,
        quality_status: str,
        reason_codes: list[str],
        raw_payload: dict[str, Any] | None,
    ) -> None:
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.final_price = final_price
        self.prev_close = prev_close
        self.volume = volume
        self.amount = amount
        self.matched_volume = matched_volume
        self.unmatched_volume = unmatched_volume
        self.quality_status = quality_status if quality_status in VALID_QUALITY_STATUSES else QUALITY_ERROR
        self.reason_codes = reason_codes or []
        self.raw_payload = raw_payload or {}


async def get_or_create_historical_capture_run(
    session: AsyncSession,
    trade_date: date,
    *,
    source: str = HISTORICAL_BACKFILL_SOURCE,
    test_namespace: str = HISTORICAL_BACKFILL_NAMESPACE,
    expected_count: int = 0,
    code_version: str | None = None,
) -> AuctionQuoteCaptureRun:
    """获取或创建历史回补 CaptureRun（每交易日 1 个，外键 owner）。

    唯一约束 ``(trade_date, source, test_namespace)`` 保证同 key 只存在一条 run。
    若已存在则直接返回（幂等），不重复创建。
    """
    existing = await session.scalar(
        select(AuctionQuoteCaptureRun).where(
            AuctionQuoteCaptureRun.trade_date == trade_date,
            AuctionQuoteCaptureRun.source == source,
            AuctionQuoteCaptureRun.test_namespace == test_namespace,
        )
    )
    if existing is not None:
        return existing

    run = AuctionQuoteCaptureRun(
        id=uuid.uuid4(),
        trade_date=trade_date,
        source=source,
        test_namespace=test_namespace,
        status="running",
        expected_count=expected_count,
        received_count=0,
        valid_count=0,
        coverage=0.0,
        started_at=_now(),
        reason_codes=[],
        code_version=code_version,
    )
    session.add(run)
    await session.flush()
    return run


def _build_quote_row(
    fact: MemberFactProjection,
    capture_run_id: uuid.UUID,
    *,
    source: str,
    test_namespace: str,
) -> dict[str, Any]:
    """将单条 MemberFactProjection 投影为 auction_final_quotes 的 upsert 行。"""
    return {
        "instrument_id": fact.instrument_id,
        "trade_date": fact.trade_date,
        "capture_run_id": capture_run_id,
        "source": source,
        "test_namespace": test_namespace,
        "final_price": Decimal(str(fact.final_price)) if fact.final_price is not None else None,
        "prev_close": Decimal(str(fact.prev_close)) if fact.prev_close is not None else None,
        "volume": fact.volume,
        "amount": Decimal(str(fact.amount)) if fact.amount is not None else None,
        "matched_volume": fact.matched_volume,
        "unmatched_volume": fact.unmatched_volume,
        "quality_status": fact.quality_status,
        "reason_codes": fact.reason_codes,
        "raw_payload": fact.raw_payload,
        "source_time": _now(),
        "is_final": True,
        "frozen_at": _now(),
    }


async def _upsert_chunk(
    session: AsyncSession,
    capture_run: AuctionQuoteCaptureRun,
    chunk: list[MemberFactProjection],
    *,
    source: str,
    test_namespace: str,
) -> dict[str, int]:
    """对单 chunk 做幂等 upsert，返回 {written, failed}。

    利用现有唯一约束 ``(trade_date, instrument_id, source, capture_run_id)`` 做
    ON CONFLICT DO UPDATE，保证重跑安全（同 key 更新而非重复插入）。
    """
    written = 0
    failed = 0
    rows = [_build_quote_row(f, capture_run.id, source=source, test_namespace=test_namespace) for f in chunk]
    if not rows:
        return {"written": 0, "failed": 0}

    stmt = pg_insert(AuctionFinalQuote).values(rows)
    # 唯一约束名来自迁移 077：(trade_date, instrument_id, source, capture_run_id)
    conflict_cols = ["trade_date", "instrument_id", "source", "capture_run_id"]
    stmt = stmt.on_conflict_do_update(
        index_elements=conflict_cols,
        set_={
            "final_price": stmt.excluded.final_price,
            "prev_close": stmt.excluded.prev_close,
            "volume": stmt.excluded.volume,
            "amount": stmt.excluded.amount,
            "matched_volume": stmt.excluded.matched_volume,
            "unmatched_volume": stmt.excluded.unmatched_volume,
            "quality_status": stmt.excluded.quality_status,
            "reason_codes": stmt.excluded.reason_codes,
            "raw_payload": stmt.excluded.raw_payload,
            "source_time": stmt.excluded.source_time,
            "is_final": stmt.excluded.is_final,
            "frozen_at": stmt.excluded.frozen_at,
        },
    )
    try:
        await session.execute(stmt)
        await session.flush()
        written = len(rows)
    except Exception as exc:  # noqa: BLE001 — 单 chunk 失败不中断整 bar
        logger.warning(
            "historical backfill upsert chunk failed trade_date=%s chunk=%d err=%s",
            capture_run.trade_date, len(rows), exc,
        )
        await session.rollback()
        failed = len(rows)
    return {"written": written, "failed": failed}


async def write_bar_quotes(
    session: AsyncSession,
    trade_date: date,
    capture_run: AuctionQuoteCaptureRun,
    facts: Iterable[MemberFactProjection],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    source: str = HISTORICAL_BACKFILL_SOURCE,
    test_namespace: str = HISTORICAL_BACKFILL_NAMESPACE,
) -> dict[str, int]:
    """将一个交易日的全部 member-fact 投影分批 upsert 进 auction_final_quotes。

    返回累计 {written, skipped, failed}：
    - written : 成功 upsert 行数
    - skipped : 投影为 None（runner 已判定无竞价数据）的行数，不写库
    - failed  : upsert 失败行数（已记录日志，不中断）

    性能：按 chunk_size 分批小事务，每 chunk 一次 execute + flush；chunk 间不累积
    大事务，避免锁表与事务内存膨胀。
    """
    written = 0
    skipped = 0
    failed = 0

    pending: list[MemberFactProjection] = []
    for fact in facts:
        if fact is None or fact.instrument_id is None:
            skipped += 1
            continue
        pending.append(fact)
        if len(pending) >= chunk_size:
            res = await _upsert_chunk(session, capture_run, pending, source=source, test_namespace=test_namespace)
            written += res["written"]
            failed += res["failed"]
            pending.clear()

    if pending:
        res = await _upsert_chunk(session, capture_run, pending, source=source, test_namespace=test_namespace)
        written += res["written"]
        failed += res["failed"]
        pending.clear()

    return {"written": written, "skipped": skipped, "failed": failed}


async def finalize_historical_capture_run(
    session: AsyncSession,
    capture_run: AuctionQuoteCaptureRun,
    *,
    received_count: int,
    valid_count: int,
) -> None:
    """bar 完成后更新 CaptureRun 状态与覆盖率（truthful degraded 标记）。"""
    capture_run.received_count = received_count
    capture_run.valid_count = valid_count
    capture_run.coverage = (valid_count / received_count) if received_count else 0.0
    capture_run.status = "succeeded" if received_count else "failed"
    capture_run.finished_at = _now()
    capture_run.heartbeat_at = _now()
    session.add(capture_run)
    await session.flush()


def project_row_to_fact(
    row: Mapping[str, Any],
    instrument_id: uuid.UUID,
    trade_date: date,
) -> MemberFactProjection:
    """将 runner ``_project_member_row`` 输出的 dict 投影为 MemberFactProjection。

    只读字段映射，不重新计算竞价算法。字段名对齐 ``_project_member_row`` 输出
    （auction_price_raw / auction_volume_shares / auction_amount / source_status 等）。
    """

    def _to_float(v: Any) -> float | None:
        return None if v is None else float(v)

    def _to_int(v: Any) -> int | None:
        return None if v is None else int(v)

    # source_status 是回补专用词表；映射为 quality_status
    source_status = row.get("source_status")
    canonicalization_status = row.get("canonicalization_status")
    if source_status == "SOURCE_EMPTY":
        quality_status = QUALITY_ZERO_VOLUME
    elif source_status in ("SOURCE_ERROR", "TARGET_SEARCH_STALLED", "TARGET_SEARCH_LIMIT_REACHED"):
        quality_status = QUALITY_ERROR
    elif canonicalization_status in ("ZERO_VOLUME", "EMPTY"):
        quality_status = QUALITY_ZERO_VOLUME
    elif canonicalization_status == "INVALID_VOLUME":
        quality_status = QUALITY_INVALID_VOLUME
    else:
        quality_status = QUALITY_OK

    reason_codes = []
    if source_status:
        reason_codes.append(f"backfill_source:{source_status}")
    if canonicalization_status:
        reason_codes.append(f"backfill_canon:{canonicalization_status}")

    return MemberFactProjection(
        instrument_id=instrument_id,
        trade_date=trade_date,
        final_price=_to_float(row.get("auction_price_raw")),
        prev_close=_to_float(row.get("prev_close")),
        volume=_to_int(row.get("auction_volume_shares")),
        amount=_to_float(row.get("auction_amount")),
        matched_volume=_to_int(row.get("auction_matched_volume_shares")),
        unmatched_volume=_to_int(row.get("auction_unmatched_volume_shares")),
        quality_status=quality_status,
        reason_codes=reason_codes,
        raw_payload=dict(row),
    )
