"""竞价最终报价采集服务 — 协调 Provider、写入 DB、维护 CaptureRun 状态（[CHANGE-20260731-001] 数据源合同）。

设计原则：
1. 09:25:05 Asia/Shanghai 后由 auction_scheduler 触发 capture，再触发 scan
2. 批量请求 + 限速（BATCH_SIZE/BATCH_INTERVAL_SECONDS 由 Provider 内部控制）
3. 失败重试：Provider 层处理 API 调用失败；本服务对 CaptureRun 状态负责
4. 字段缺失/停牌/零成交/调用失败写 quality_status/reason_codes
5. 不把接口返回自动视为真值；保存原始 source_time 和 raw_payload
6. 使用 test_namespace 和 capture_run_id 隔离测试数据，不污染生产

约束：
- 同 (trade_date, source, test_namespace) 已 succeeded → 幂等返回，不重复采集
- 同 (trade_date, source, test_namespace) running 且租约有效 → 抛 ConflictError
- 每只股票写一条 AuctionFinalQuote（quality_status 区分有效/无效）
- coverage = valid_count / expected_count

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_quote_capture_service
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import AuctionFinalQuote, AuctionQuoteCaptureRun
from app.models.instrument import Instrument
from app.services.auction_quote_provider import (
    AuctionFinalQuoteProvider,
    AuctionQuoteResult,
    MootdxAuctionQuoteProvider,
)

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

# 默认数据源（项目已有 pytdx/mootdx，不新增 AKShare/东财）
DEFAULT_SOURCE = "mootdx"

# 生产命名空间（非 Canary 使用）
PRODUCTION_NAMESPACE = "production"

# 租约过期阈值（秒）：超过此值未 heartbeat 的 run 视为僵尸，可被 fencing
CAPTURE_RUN_LEASE_SECONDS = 600  # 10 分钟（采集应 2-5 分钟内完成）
CAPTURE_RUN_HEARTBEAT_INTERVAL_SECONDS = 30

# 代码版本（由调用方传入，或取 git short sha）
DEFAULT_CODE_VERSION = "dev"


# =============================================================================
# 异常
# =============================================================================


class AuctionCaptureConflictError(ValueError):
    """同 (trade_date, source, namespace) 仍有 running 且租约有效，拒绝重复执行。"""


class AuctionCaptureAlreadySucceededError(ValueError):
    """同 (trade_date, source, namespace) 已 succeeded，幂等拒绝重复执行。"""


# =============================================================================
# 主入口：capture_auction_final_quotes
# =============================================================================


async def capture_auction_final_quotes(
    db: AsyncSession,
    trade_date: date,
    *,
    test_namespace: str = PRODUCTION_NAMESPACE,
    provider: AuctionFinalQuoteProvider | None = None,
    code_version: str = DEFAULT_CODE_VERSION,
    worker_id: str | None = None,
    expected_symbols: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """采集最终竞价报价并写入 auction_final_quotes。

    流程：
    1. 幂等获取/创建 AuctionQuoteCaptureRun（status=running）
    2. 如 expected_symbols 未指定，加载所有活跃 A 股
    3. 调用 Provider.fetch_auction_quotes 批量获取
    4. 将结果写入 auction_final_quotes（含 quality_status/reason_codes/raw_payload）
    5. 更新 CaptureRun 的 received/valid/coverage 和 status=succeeded/failed/partial

    Args:
        db: 异步 DB 会话（不 commit，由调用方控制事务）
        trade_date: 业务交易日
        test_namespace: 隔离命名空间（production / auction_v1_canary_<date>_<sha>）
        provider: 数据源 Provider（None 使用默认 MootdxAuctionQuoteProvider）
        code_version: 代码版本标识
        worker_id: Worker 标识
        expected_symbols: 预期采集的 (symbol, market) 列表（Canary 模式使用）

    Returns:
        {
            "capture_run_id": uuid.UUID,
            "status": str,
            "expected_count": int,
            "received_count": int,
            "valid_count": int,
            "coverage": float,
            "reason_codes": list[str],
        }
    """
    logger.info(
        "[AuctionCapture] 开始采集: trade_date=%s namespace=%s worker_id=%s",
        trade_date, test_namespace, worker_id,
    )

    # 1. 幂等获取/创建 CaptureRun
    now = datetime.now(UTC)
    source = getattr(provider, "source_id", DEFAULT_SOURCE) if provider is not None else DEFAULT_SOURCE
    run = await _acquire_or_get_capture_run(
        db, trade_date, source=source,
        test_namespace=test_namespace, code_version=code_version,
        worker_id=worker_id, now=now,
    )

    # run.status == "succeeded" → 幂等返回
    if run.status == "succeeded":
        logger.info(
            "[AuctionCapture] 幂等命中已成功 run: %s, coverage=%.4f",
            run.id, run.coverage,
        )
        return _build_capture_summary(run)

    # 2. 加载预期股票列表
    if expected_symbols is None:
        instruments = await _get_active_a_share_instruments(db)
        expected_symbols = [(inst.symbol, inst.market) for inst in instruments]
        instrument_id_map: dict[tuple[str, str], uuid.UUID] = {
            (inst.symbol, inst.market): inst.id for inst in instruments
        }
    else:
        # Canary 模式：根据 expected_symbols 查询 instrument_id
        instrument_id_map = await _resolve_instrument_ids(db, expected_symbols)

    # 更新 expected_count
    run.expected_count = len(expected_symbols)
    run.heartbeat_at = now
    await db.flush()

    if not expected_symbols:
        logger.warning("[AuctionCapture] 无预期股票，标记 succeeded (empty)")
        run.status = "succeeded"
        run.received_count = 0
        run.valid_count = 0
        run.coverage = 0.0
        run.finished_at = datetime.now(UTC)
        await db.flush()
        return _build_capture_summary(run)

    # 3. 调用 Provider 批量获取
    provider_owned = False
    if provider is None:
        provider = MootdxAuctionQuoteProvider()
        provider_owned = True

    try:
        results = await _fetch_with_retry(provider, expected_symbols)
    finally:
        if provider_owned:
            provider.close()

    # 4. 写入 auction_final_quotes
    received_count = len(results)
    valid_count = 0
    reason_counter: dict[str, int] = {}

    for result in results:
        instrument_id = instrument_id_map.get((result.symbol, result.market))
        if instrument_id is None:
            logger.warning(
                "[AuctionCapture] 无法解析 instrument_id: symbol=%s market=%s",
                result.symbol, result.market,
            )
            continue

        # 解析 source_time（pytdx servertime 通常是 "HH:MM:SS" 字符串）
        source_time = _parse_source_time(result.servertime, trade_date)

        # 构造 raw_payload（用于审计和回溯）
        raw_payload = result.raw_payload if result.raw_payload is not None else None

        # 转换数值字段
        final_price = _safe_decimal(result.price)
        prev_close = _safe_decimal(result.last_close)
        volume = _safe_int(result.volume)
        amount = _safe_decimal(result.amount)

        if result.is_valid:
            valid_count += 1

        for code in result.reason_codes:
            reason_counter[code] = reason_counter.get(code, 0) + 1

        quote = AuctionFinalQuote(
            trade_date=trade_date,
            instrument_id=instrument_id,
            capture_run_id=run.id,
            test_namespace=test_namespace,
            source=source,
            source_server=result.source_server,
            source_time=source_time,
            final_price=final_price,
            prev_close=prev_close,
            volume=volume,
            amount=amount,
            matched_volume=None,  # pytdx 不直接返回匹配量
            unmatched_volume=None,
            captured_at=result.captured_at,
            is_final=result.is_final_auction,
            quality_status=result.quality_status,
            reason_codes=list(result.reason_codes),
            raw_payload=raw_payload,
        )
        db.add(quote)

    # 5. 更新 CaptureRun 统计
    coverage = (valid_count / run.expected_count) if run.expected_count > 0 else 0.0
    if valid_count == 0:
        final_status = "failed"
    elif valid_count < run.expected_count:
        final_status = "partial"
    else:
        final_status = "succeeded"

    run.received_count = received_count
    run.valid_count = valid_count
    run.coverage = round(coverage, 6)
    run.status = final_status
    run.finished_at = datetime.now(UTC)
    run.reason_codes = [
        f"{code}:{count}" for code, count in sorted(reason_counter.items())
    ][:20]  # 最多 20 条 reason summary
    await db.flush()

    logger.info(
        "[AuctionCapture] 采集完成: run_id=%s status=%s expected=%d received=%d "
        "valid=%d coverage=%.4f",
        run.id, final_status, run.expected_count, received_count,
        valid_count, coverage,
    )

    return _build_capture_summary(run)


# =============================================================================
# CaptureRun 获取/创建（幂等）
# =============================================================================


async def _acquire_or_get_capture_run(
    db: AsyncSession,
    trade_date: date,
    *,
    source: str,
    test_namespace: str,
    code_version: str,
    worker_id: str | None,
    now: datetime,
) -> AuctionQuoteCaptureRun:
    """幂等获取/创建 AuctionQuoteCaptureRun。

    合同：
    - 同 (trade_date, source, namespace) 已 succeeded → 返回该 run，标记幂等命中
    - running 且租约有效 → 抛 AuctionCaptureConflictError
    - running 但租约过期 → fencing：原子更新 worker_id，继续执行
    - 无记录或 failed → 创建新 run

    Args:
        db: 异步会话
        trade_date: 业务交易日
        source: 数据源
        test_namespace: 隔离命名空间
        code_version: 代码版本
        worker_id: Worker 标识
        now: 当前时间

    Returns:
        AuctionQuoteCaptureRun（status=running 或 succeeded）
    """
    stmt = (
        select(AuctionQuoteCaptureRun)
        .where(
            AuctionQuoteCaptureRun.trade_date == trade_date,
            AuctionQuoteCaptureRun.source == source,
            AuctionQuoteCaptureRun.test_namespace == test_namespace,
        )
        .order_by(AuctionQuoteCaptureRun.created_at.desc())
        .limit(1)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing is not None:
        if existing.status == "succeeded":
            return existing

        if existing.status == "running":
            # 检查租约是否过期
            if _is_lease_expired(existing.heartbeat_at, now=now):
                logger.warning(
                    "[AuctionCapture] 检测到僵尸 run，fencing 接管: %s (旧 worker=%s)",
                    existing.id, existing.id,
                )
                existing.status = "running"
                existing.started_at = now
                existing.heartbeat_at = now
                # 注意：CaptureRun 无 lease_epoch 字段，依靠 heartbeat_at 判定
                await db.flush()
                return existing
            raise AuctionCaptureConflictError(
                f"trade_date={trade_date} source={source} namespace={test_namespace} "
                f"仍有 running run (id={existing.id})，租约有效，拒绝重复执行"
            )

        # failed/partial → 在唯一键对应的同一 run 上重试，并清理半成品报价。
        await db.execute(
            delete(AuctionFinalQuote).where(AuctionFinalQuote.capture_run_id == existing.id)
        )
        existing.status = "running"
        existing.expected_count = 0
        existing.received_count = 0
        existing.valid_count = 0
        existing.coverage = 0.0
        existing.started_at = now
        existing.finished_at = None
        existing.heartbeat_at = now
        existing.reason_codes = ["capture_retry"]
        existing.code_version = code_version
        await db.flush()
        return existing

    # 创建新 run
    run = AuctionQuoteCaptureRun(
        trade_date=trade_date,
        source=source,
        test_namespace=test_namespace,
        status="running",
        expected_count=0,
        received_count=0,
        valid_count=0,
        coverage=0.0,
        started_at=now,
        heartbeat_at=now,
        code_version=code_version,
        reason_codes=[],
    )
    db.add(run)
    await db.flush()
    return run


def _is_lease_expired(
    heartbeat_at: datetime | None,
    *,
    now: datetime | None = None,
    expired_seconds: int = CAPTURE_RUN_LEASE_SECONDS,
) -> bool:
    """判定租约是否已过期。"""
    if heartbeat_at is None:
        return True
    current = now or datetime.now(UTC)
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
    delta = (current - heartbeat_at).total_seconds()
    return delta > expired_seconds


def _build_capture_summary(run: AuctionQuoteCaptureRun) -> dict[str, Any]:
    """从 CaptureRun 构造返回结果。"""
    return {
        "capture_run_id": run.id,
        "status": run.status,
        "expected_count": run.expected_count,
        "received_count": run.received_count,
        "valid_count": run.valid_count,
        "coverage": run.coverage,
        "reason_codes": list(run.reason_codes or []),
        "idempotent": run.status == "succeeded",
    }


# =============================================================================
# 辅助函数
# =============================================================================


async def _get_active_a_share_instruments(db: AsyncSession) -> list[Instrument]:
    """获取所有活跃 A 股 instrument（symbol 6 位数字）。"""
    stmt = select(Instrument).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _resolve_instrument_ids(
    db: AsyncSession,
    symbols: list[tuple[str, str]],
) -> dict[tuple[str, str], uuid.UUID]:
    """根据 (symbol, market) 列表查询 instrument_id。

    Returns: dict[(symbol, market), instrument_id]
    """
    if not symbols:
        return {}
    stmt = select(Instrument).where(
        Instrument.status == "active",
        Instrument.symbol.in_([s for s, _ in symbols]),
    )
    result = await db.execute(stmt)
    return {(inst.symbol, inst.market): inst.id for inst in result.scalars().all()}


async def _fetch_with_retry(
    provider: AuctionFinalQuoteProvider,
    symbols: list[tuple[str, str]],
    *,
    max_retries: int = 1,
) -> list[AuctionQuoteResult]:
    """调用 Provider.fetch_auction_quotes，支持简单重试。

    Provider 内部已处理单批失败，本函数只在整体调用失败时重试一次。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            # Provider.fetch_auction_quotes 是同步方法，使用 asyncio.to_thread 包装
            return await asyncio.to_thread(provider.fetch_auction_quotes, symbols)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[AuctionCapture] Provider 调用失败 (attempt=%d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )
    if last_exc is not None:
        # 最后一次失败，返回空列表并记录（CaptureRun 会被标记为 failed）
        logger.error(
            "[AuctionCapture] Provider 重试耗尽，返回空结果: %s", last_exc,
        )
        return []
    return []


def _parse_source_time(
    servertime: str | None,
    trade_date: date,
) -> datetime | None:
    """解析 pytdx servertime（如 "9:25:5" 或 "09:25:05"）为 datetime。

    Args:
        servertime: pytdx 返回的 servertime 字符串
        trade_date: 业务交易日（用于构造完整 datetime）

    Returns:
        带时区的 datetime（Asia/Shanghai），None 表示解析失败
    """
    if servertime is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz_sh = ZoneInfo("Asia/Shanghai")
        # 处理 "9:25:5" 或 "09:25:05" 格式
        parts = str(servertime).split(":")
        if len(parts) != 3:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(float(parts[2]))
        return datetime(
            trade_date.year, trade_date.month, trade_date.day,
            hour, minute, second, tzinfo=tz_sh,
        )
    except (ValueError, TypeError):
        return None


def _safe_decimal(v: Any) -> Decimal | None:
    """安全转换为 Decimal，None/非数值返回 None。"""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        if not d.is_finite():
            return None
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_int(v: Any) -> int | None:
    """安全转换为 int，None/非数值返回 None。"""
    if v is None:
        return None
    try:
        # pytdx vol 可能是 float
        f = float(v)
        return int(f) if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


# =============================================================================
# 查询接口
# =============================================================================


async def get_capture_run_for_date(
    db: AsyncSession,
    trade_date: date,
    *,
    test_namespace: str = PRODUCTION_NAMESPACE,
    source: str = DEFAULT_SOURCE,
) -> AuctionQuoteCaptureRun | None:
    """查询指定交易日的 CaptureRun（取最新一条）。"""
    stmt = (
        select(AuctionQuoteCaptureRun)
        .where(
            AuctionQuoteCaptureRun.trade_date == trade_date,
            AuctionQuoteCaptureRun.source == source,
            AuctionQuoteCaptureRun.test_namespace == test_namespace,
        )
        .order_by(AuctionQuoteCaptureRun.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def load_final_quotes_for_scan(
    db: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
    *,
    test_namespace: str = PRODUCTION_NAMESPACE,
    source: str | None = None,
) -> dict[uuid.UUID, AuctionFinalQuote]:
    """[auction_scan_service 调用] 加载最终竞价报价，替代 _load_final_auction_bars。

    仅返回 quality_status=ok 的报价（停牌/零成交的仍返回，由 scan_service 决定如何处理）。

    Returns: dict[instrument_id, AuctionFinalQuote]
    """
    if not instrument_ids:
        return {}

    # 查询最新成功的 CaptureRun
    if source is None:
        from app.services.auction_truth_service import VERIFIED_AUCTION_SOURCE

        source = VERIFIED_AUCTION_SOURCE
    run = await get_capture_run_for_date(
        db, trade_date, test_namespace=test_namespace, source=source,
    )
    if run is None or run.status not in ("succeeded", "partial"):
        return {}

    stmt = (
        select(AuctionFinalQuote)
        .where(
            AuctionFinalQuote.capture_run_id == run.id,
            AuctionFinalQuote.instrument_id.in_(instrument_ids),
        )
    )
    result = await db.execute(stmt)
    return {q.instrument_id: q for q in result.scalars().all()}


async def load_history_final_quotes(
    db: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
    *,
    lookback: int = 20,
    test_namespace: str = PRODUCTION_NAMESPACE,
    source: str | None = None,
) -> dict[uuid.UUID, list[AuctionFinalQuote]]:
    """加载 trade_date 前 lookback 个交易日的竞价报价历史。

    Returns: dict[instrument_id, list[AuctionFinalQuote]] 按交易日期降序（最新在前）
    """
    if not instrument_ids:
        return {}

    if source is None:
        from app.services.auction_truth_service import VERIFIED_AUCTION_SOURCE

        source = VERIFIED_AUCTION_SOURCE

    # 查询历史 CaptureRuns（仅 succeeded/partial）
    calendar_lookback = lookback * 2 + 5
    start_date = trade_date - timedelta(days=calendar_lookback)

    stmt = (
        select(AuctionQuoteCaptureRun)
        .where(
            AuctionQuoteCaptureRun.trade_date < trade_date,
            AuctionQuoteCaptureRun.trade_date >= start_date,
            AuctionQuoteCaptureRun.source == source,
            AuctionQuoteCaptureRun.test_namespace == test_namespace,
            AuctionQuoteCaptureRun.status.in_(["succeeded", "partial"]),
        )
        .order_by(AuctionQuoteCaptureRun.trade_date.desc())
    )
    runs = (await db.execute(stmt)).scalars().all()
    if not runs:
        return {}

    run_ids = [r.id for r in runs]

    # 查询这些 runs 下的 quotes
    quote_stmt = (
        select(AuctionFinalQuote)
        .where(
            AuctionFinalQuote.capture_run_id.in_(run_ids),
            AuctionFinalQuote.instrument_id.in_(instrument_ids),
            AuctionFinalQuote.quality_status == "ok",
        )
        .order_by(
            AuctionFinalQuote.instrument_id,
            AuctionFinalQuote.trade_date.desc(),
        )
    )
    result = await db.execute(quote_stmt)
    history_map: dict[uuid.UUID, list[AuctionFinalQuote]] = {}
    for quote in result.scalars().all():
        history_map.setdefault(quote.instrument_id, []).append(quote)
    return {iid: quotes[:lookback] for iid, quotes in history_map.items()}


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    import os

    if os.environ.get("PURE_UNIT_TEST"):
        # 纯单元测试：不连接 DB
        # 1. _parse_source_time
        from datetime import date as _date
        dt = _parse_source_time("9:25:5", _date(2026, 7, 31))
        assert dt is not None, "expected valid datetime"
        assert dt.hour == 9 and dt.minute == 25 and dt.second == 5
        print("[PASS] _parse_source_time")

        dt2 = _parse_source_time("09:25:05", _date(2026, 7, 31))
        assert dt2 is not None and dt2.hour == 9 and dt2.minute == 25
        print("[PASS] _parse_source_time (zero-padded)")

        assert _parse_source_time(None, _date(2026, 7, 31)) is None
        assert _parse_source_time("invalid", _date(2026, 7, 31)) is None
        print("[PASS] _parse_source_time (invalid)")

        # 2. _safe_decimal
        assert _safe_decimal(None) is None
        assert _safe_decimal("10.5") == Decimal("10.5")
        assert _safe_decimal("invalid") is None
        print("[PASS] _safe_decimal")

        # 3. _safe_int
        assert _safe_int(None) is None
        assert _safe_int(100.0) == 100
        assert _safe_int("invalid") is None
        print("[PASS] _safe_int")

        # 4. _is_lease_expired
        now = datetime(2026, 7, 31, 2, 0, 0, tzinfo=UTC)
        hb = datetime(2026, 7, 31, 1, 49, 0, tzinfo=UTC)
        assert _is_lease_expired(hb, now=now, expired_seconds=600) is True  # 11分钟过期
        hb2 = datetime(2026, 7, 31, 1, 55, 0, tzinfo=UTC)
        assert _is_lease_expired(hb2, now=now, expired_seconds=600) is False  # 5分钟未过期
        assert _is_lease_expired(None, now=now) is True
        print("[PASS] _is_lease_expired")

        # 5. 常量校验
        assert DEFAULT_SOURCE == "mootdx"
        assert PRODUCTION_NAMESPACE == "production"
        assert CAPTURE_RUN_LEASE_SECONDS == 600
        print("[PASS] 常量校验")

        print("[PASS] 所有模块自测通过")
