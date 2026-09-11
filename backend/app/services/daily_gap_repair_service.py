"""market-wide 单日缺口修复 owner（只针对「整日空洞」这一类 P0 缺口）。

背景：
    09-10 这种**整日缺失**（coverage == 0）不能走 ``fill_missing_daily_instruments``：
    那会变成 5000+ 次「逐股新建 AsyncClient + 逐股 commit」的数小时任务。

本模块的契约：
- **只修一个 trade_date**：一次调用只补一个交易日，不顺手补别的日子。
- **数据源顺序：同花顺不复权 → Eastmoney fqt=0**。原定「Eastmoney 优先」，但实测生产
  出口的 ``push2his`` 已被东财 IP 级硬封（并发热身后全为 ``RemoteProtocolError``），
  恢复期吞吐仅 ~0.2 成功请求/秒 —— 5293 只要 7 小时以上，方案不成立。同花顺
  ``/00/`` 经 A/B 验证为逐位一致的不复权源（OHLC 坏点 0/172、amount 全通过），
  且实测 15.8 req/s。Eastmoney 保留为次选，用于同花顺不可用的兜底。
  不调用 pytdx —— pytdx 是当前故障源，且它的 raw 历史与 canonical EOD 口径不同。
- **一个共享 AsyncClient + 有界并发**：``httpx.Limits`` 限制连接数，
  ``asyncio.Semaphore`` 限制在途请求。并发默认 3：实测 2/3/4 并发下 3 的
  成功率最高（99.33%）、4 反而回落到 97.33%（触发站点 502 限流）。
- **批量写**：按 chunk 做一次 ``pg_insert ... on_conflict_do_nothing`` + 一次 commit，
  绝不逐股 commit。
- **exact-date 校验**：请求区间收口到 [T, T]，且必须恰好拿到 1 根 T 日 bar；
  否则该股计入 failed，不写库。
- **可中断、可重跑、幂等**：重复运行只会补仍然缺失的行（DB 是唯一事实源）。
- **不覆盖既有行**：``on_conflict_do_nothing``，因此绝不改写 09-09 / 09-11 或任何
  既有行的 OHLCV 与 ``adj_factor``。

单位（与 canonical 对齐，见 ths_raw_daily_provider 与
eod_market_snapshot_provider.SHARES_PER_LOT 的实测证据）：
    ``volume`` = 股，``amount`` = 元。同花顺原始返回即为该口径，不缩放。

写库前置门禁：
    生产写入前必须先跑 :func:`compare_db_vs_ths_for_date`（只读 A/B）并通过
    :func:`validate_consistency`；否则禁止写 09-10（见 :class:`SourceConsistencyError`）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.services.eod_market_snapshot_provider import fetch_eastmoney_daily_kline
from app.services.instrument_maintenance_service import stock_symbol_sql_filter
from app.services.ths_raw_daily_provider import fetch_ths_raw_daily

logger = logging.getLogger(__name__)

# 价格一致阈值：绝对差 <= 0.01 元视为一致。
_PRICE_TOLERANCE = Decimal("0.01")
# 成交量一致阈值：绝对差 <= 100 股视为一致。
# canonical volume 单位是「股」（量级 10^6~10^9），100 股等价相对误差 <= 1e-4。
# 实测两侧最大绝对差为 12 股（一手取整残留），远低于该阈值。
_VOLUME_TOLERANCE = Decimal("100")
# 成交额坏点：绝对差 > 1000 元 **且** 相对差 > 0.1% 才算不一致
# （供应商统计/四舍五入差异不判错）。
_AMOUNT_ABS_TOLERANCE = Decimal("1000")
_AMOUNT_REL_TOLERANCE = Decimal("0.001")
# 一致性门禁：坏点比例超过 1% 即禁止写入。
_CONSISTENCY_BAD_RATIO_LIMIT = 0.01
# 一致性门禁（覆盖率）：抽到的样本里成功抓取的比例必须 >= 95%，
# 否则「3/200 抓到也一致」这类抓取失败被误判为通过的情况必须被挡住。
_MIN_FETCH_SUCCESS_RATIO = 0.95
# 一致性门禁（可比覆盖率）：抓取成功样本中，OHLC/volume/amount 任一项
# 真正参与比较的比例必须 >= 95%（例如 200 抓到但只有 100 可比，也 FAIL）。
_MIN_COMPARISON_RATIO = 0.95

# A/B 抽样规模（按市场分层；北交所不足则全取）。
_AB_SAMPLE_SH = 80
_AB_SAMPLE_SZ = 80
_AB_SAMPLE_BJ = 40

# 并发 3 是实测最优点：2 太慢（2.9 req/s），4 触发限流回落（97.33%），
# 3 达 99.33% / 15.8 req/s。
_DEFAULT_CONCURRENCY = 3
_DEFAULT_CHUNK_SIZE = 200


class SourceConsistencyError(RuntimeError):
    """数据源一致性门禁失败：禁止把该交易日的修复结果写入生产库。"""


@dataclass
class MarketWideRepairResult:
    """market-wide 单日修复结果。"""

    trade_date: date
    eligible: int
    initially_missing: int

    requested: int = 0
    fetched: int = 0
    inserted: int = 0
    verified: int = 0
    still_missing: int = 0
    coverage_after: float = 0.0

    # 按数据源统计成功取到的只数（用于判断主源是否退化）。
    ths_fetched: int = 0
    eastmoney_fetched: int = 0
    eastmoney_failed: int = 0

    failed_symbols: list[str] = field(default_factory=list)
    error_samples: list[str] = field(default_factory=list)
    dry_run: bool = False
    elapsed_seconds: float = 0.0


@dataclass
class ConsistencyReport:
    """Eastmoney fqt=0 与 DB ``bars_daily`` 的 A/B 一致性报告（只读）。"""

    trade_date: date
    # 本报告所验证的「邻近已知完整交易日」（禁止拿待修日自己验证自己）。
    # repair owner 写生产库前会检查 reference_trade_date < 待修 trade_date。
    reference_trade_date: date = date(2000, 1, 1)
    sample_requested: int = 0
    fetch_succeeded: int = 0
    fetch_failed: int = 0
    missing_in_db: int = 0

    ohlc_compared: int = 0
    ohlc_exact: int = 0
    ohlc_bad: int = 0
    ohlc_max_abs_diff: Decimal = Decimal("0")
    ohlc_bad_ratio: float = 0.0

    volume_compared: int = 0
    volume_exact: int = 0
    volume_bad: int = 0
    volume_max_abs_diff: Decimal = Decimal("0")
    volume_bad_ratio: float = 0.0

    amount_compared: int = 0
    amount_bad: int = 0
    amount_p50_rel: float = 0.0
    amount_p95_rel: float = 0.0
    amount_p99_rel: float = 0.0
    amount_max_rel: float = 0.0
    amount_bad_ratio: float = 0.0

    # 被验证的数据源标识（"ths" / "eastmoney"），用于审计与门禁追溯。
    source: str = ""

    failed_symbols: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"trade_date={self.trade_date} sample={self.sample_requested} "
            f"fetch_ok={self.fetch_succeeded} fetch_fail={self.fetch_failed} "
            f"| OHLC compared={self.ohlc_compared} exact={self.ohlc_exact} "
            f"bad={self.ohlc_bad} ratio={self.ohlc_bad_ratio:.4f} "
            f"max_abs={self.ohlc_max_abs_diff} "
            f"| VOL compared={self.volume_compared} exact={self.volume_exact} "
            f"bad={self.volume_bad} ratio={self.volume_bad_ratio:.4f} "
            f"max_abs={self.volume_max_abs_diff} "
            f"| AMT compared={self.amount_compared} bad={self.amount_bad} "
            f"ratio={self.amount_bad_ratio:.4f} "
            f"p50={self.amount_p50_rel:.6f} p95={self.amount_p95_rel:.6f} "
            f"p99={self.amount_p99_rel:.6f} max={self.amount_max_rel:.6f}"
        )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """简单线性插值分位数（values 必须已升序）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return float(
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac
    )


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 供应商脏值一律当缺失
        return None


async def _evenly_sample_instruments(
    session: AsyncSession,
    trade_date: date,
    market: str,
    limit: int,
) -> list[Instrument]:
    """从「当日在 DB 已有 bar 且 market 匹配」的活跃 A 股中确定性等距抽样。"""
    covered = select(BarDaily.instrument_id).where(BarDaily.trade_date == trade_date)
    stmt = (
        select(Instrument)
        .where(Instrument.status == "active")
        .where(Instrument.market == market)
        .where(stock_symbol_sql_filter(Instrument))
        .where(Instrument.id.in_(covered))
        .order_by(Instrument.symbol)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[min(int(i * step), len(rows) - 1)] for i in range(limit)]


async def _exact_bar_from_source(
    fetch: Any,
    client: httpx.AsyncClient,
    inst: Instrument,
    trade_date: date,
) -> tuple[dict | None, str | None]:
    """调用一个 provider，取回**恰好一根** T 日 bar。"""
    try:
        records = await fetch()
    except Exception as exc:  # noqa: BLE001 - provider 任意失败都计入该股失败
        return None, f"{type(exc).__name__}: {exc}"
    exact = [r for r in records if str(r.get("datetime")) == trade_date.isoformat()]
    if len(exact) != 1:
        return None, f"expected exactly one {trade_date} bar, got={len(exact)}"
    return exact[0], None


async def _fetch_t_bar(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    inst: Instrument,
    trade_date: date,
    *,
    use_eastmoney_fallback: bool = True,
) -> tuple[Instrument, dict | None, str | None, str | None]:
    """拉取单只标的 T 日 bar；必须恰好 1 根 T 日记录。

    数据源顺序：**同花顺不复权**优先（实测唯一可用的批量源），
    Eastmoney ``fqt=0`` 兜底（当前生产出口被东财硬封，通常不可用）。

    Args:
        use_eastmoney_fallback: 是否启用 Eastmoney 兜底。实测东财对生产出口
            IP 硬封时，每一次兜底都要跑满 3 轮 × 多主机重试（单只 10~20s）。
            5293 只标的里只要有几百只走到兜底，整轮修复就会从分钟级退化到小时级。
            因此该兜底必须可关：东财确认不可用期间由操作员显式关闭。

    Returns:
        (instrument, record|None, error|None, source|None)
    """
    async with semaphore:
        record, error = await _exact_bar_from_source(
            lambda: fetch_ths_raw_daily(client, inst.symbol, trade_date, trade_date),
            client,
            inst,
            trade_date,
        )
        if record is not None:
            return inst, record, None, "ths"

        first_error = error or "no-data"
        if not use_eastmoney_fallback:
            return inst, None, f"ths={first_error}", None
        record, error = await _exact_bar_from_source(
            lambda: fetch_eastmoney_daily_kline(
                client, inst.symbol, inst.market, trade_date, trade_date
            ),
            client,
            inst,
            trade_date,
        )
        if record is not None:
            return inst, record, None, "eastmoney"
        return inst, None, f"ths={first_error}; em={error or 'no-data'}", None


async def bulk_insert_raw_daily_repair(
    session: AsyncSession,
    rows: Sequence[tuple[UUID, dict]],
    trade_date: date,
) -> int:
    """批量 insert raw 日线（``on_conflict_do_nothing``），返回**真实写入**的行数。

    - 只接受 ``datetime == trade_date`` 的记录；
    - 只接受通过价格结构校验（OHLC > 0、high/low 关系、volume/amount >= 0）的记录；
    - ``on_conflict_do_nothing``：绝不覆盖既有行（含 09-09 / 09-11 与既有 adj_factor）；
    - 通过 ``RETURNING`` 取回实际被插入的行的主键，**真实**计数（重跑时可能 0 行，
      因为缺失行已在上一轮补齐、其余均 conflict 跳过）。
    """
    records: list[dict] = []
    for instrument_id, raw in rows:
        if str(raw.get("datetime")) != trade_date.isoformat():
            continue

        o = _to_decimal(raw.get("open"))
        h = _to_decimal(raw.get("high"))
        lo = _to_decimal(raw.get("low"))
        c = _to_decimal(raw.get("close"))
        v = _to_decimal(raw.get("volume"))
        a = _to_decimal(raw.get("amount"))

        if o is None or h is None or lo is None or c is None:
            continue
        if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
            continue
        if h < max(o, c):
            continue
        if lo > min(o, c):
            continue
        if v is None or v < 0:
            continue
        if a is None or a < 0:
            continue

        records.append(
            {
                "instrument_id": instrument_id,
                "trade_date": trade_date,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
                "amount": a,
                # 仅首插时使用；on_conflict_do_nothing 不更新任何既有列。
                "adj_factor": Decimal("1"),
            }
        )

    if not records:
        return 0

    stmt = pg_insert(BarDaily).values(records)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["instrument_id", "trade_date"]
    ).returning(BarDaily.instrument_id)
    res = await session.execute(stmt)
    inserted = len(res.fetchall())
    await session.commit()
    return inserted


async def repair_market_wide_daily_gap(
    session: AsyncSession,
    trade_date: date,
    *,
    consistency_report: ConsistencyReport | None = None,
    concurrency: int = _DEFAULT_CONCURRENCY,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
    use_eastmoney_fallback: bool = False,
) -> MarketWideRepairResult:
    """修复某一个交易日的**整日/大面积**日线缺口（同花顺不复权，批量）。

    设计要点见模块 docstring。``dry_run=True`` 时仍然真实拉取（用于确认 fetch 成功率），
    但不写库。

    写库门禁（fail-closed，无生产绕过开关）：
        生产写入（``dry_run=False``）**强制**要求一份已通过
        :func:`validate_consistency` 的 :class:`ConsistencyReport`，且其
        ``reference_trade_date`` 必须早于 ``trade_date``（禁止拿待修日自己验证自己），
        且 ``source`` 必须是 ``"ths"``（全市场修复主源）。``dry_run=True`` 可无 report。
        不存在 ``enforce_consistency_gate=False`` 这类生产绕过参数。

    Args:
        session: 异步 DB 会话。
        trade_date: 唯一目标交易日。
        consistency_report: 邻近已知完整交易日的 A/B 一致性报告（生产写入必填）。
        concurrency: 在途请求上限（同时限制 httpx 连接池）。默认 3（实测最优）。
        chunk_size: 每批写库的标的数（一次 insert + 一次 commit）。
        dry_run: True=只拉取与统计，不写库。
        client: 复用外部 client（测试注入用）；None 时内部创建。
        use_eastmoney_fallback: 见 :func:`_fetch_t_bar`。当前生产事实是
            THS 主用、Eastmoney 出口被封，故默认 **False**（不靠人工记得传 False）。
    """
    from app.services.eod_daily_refresh_service import (
        count_active_a_share_instruments,
        find_missing_daily_instruments,
    )

    # —— 写库前置门禁：fail-closed（无生产绕过开关）——
    if not dry_run:
        if consistency_report is None:
            raise SourceConsistencyError(
                "production repair requires a validated consistency report "
                "(pass consistency_report=compare_db_vs_ths_for_date(...))"
            )
        validate_consistency(consistency_report)
        if consistency_report.reference_trade_date >= trade_date:
            raise SourceConsistencyError(
                "consistency reference date must be before repair trade_date: "
                f"reference={consistency_report.reference_trade_date} "
                f">= repair={trade_date}"
            )
        if consistency_report.source != "ths":
            raise SourceConsistencyError(
                "market-wide repair primary source is THS; "
                f"consistency report source={consistency_report.source!r}"
            )

    started = time.monotonic()
    result = MarketWideRepairResult(trade_date=trade_date, eligible=0, initially_missing=0)
    result.dry_run = dry_run

    eligible = await count_active_a_share_instruments(session)
    missing = await find_missing_daily_instruments(session, trade_date)
    result.eligible = eligible
    result.initially_missing = len(missing)
    result.requested = len(missing)

    logger.info(
        "[GAP-REPAIR] trade_date=%s eligible=%d missing=%d concurrency=%d "
        "dry_run=%s eastmoney_fallback=%s",
        trade_date, eligible, len(missing), concurrency, dry_run, use_eastmoney_fallback,
    )

    if not missing:
        result.still_missing = 0
        result.verified = 0
        result.coverage_after = (eligible - 0) / eligible if eligible else 0.0
        result.elapsed_seconds = time.monotonic() - started
        return result

    semaphore = asyncio.Semaphore(max(concurrency, 1))
    limits = httpx.Limits(
        max_connections=max(concurrency, 1),
        max_keepalive_connections=max(concurrency, 1),
    )
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0)

    active_client = client or httpx.AsyncClient(limits=limits, timeout=timeout)
    try:
        items = list(missing)
        for idx in range(0, len(items), chunk_size):
            chunk = items[idx : idx + chunk_size]
            fetched = await asyncio.gather(
                *(
                    _fetch_t_bar(
                        active_client,
                        semaphore,
                        inst,
                        trade_date,
                        use_eastmoney_fallback=use_eastmoney_fallback,
                    )
                    for inst in chunk
                )
            )

            good: list[tuple[UUID, dict]] = []
            for inst, record, error, source in fetched:
                if record is None:
                    result.failed_symbols.append(inst.symbol)
                    if error is not None and len(result.error_samples) < 10:
                        result.error_samples.append(f"{inst.symbol}: {error}")
                    continue
                if source == "ths":
                    result.ths_fetched += 1
                elif source == "eastmoney":
                    result.eastmoney_fetched += 1
                good.append((inst.id, record))

            result.fetched += len(good)

            if not dry_run and good:
                result.inserted += await bulk_insert_raw_daily_repair(
                    session, good, trade_date
                )

            logger.info(
                "[GAP-REPAIR] chunk %d/%d fetched=%d inserted_total=%d failed=%d",
                idx // chunk_size + 1,
                (len(items) + chunk_size - 1) // chunk_size,
                len(good),
                result.inserted,
                len(result.failed_symbols),
            )
    finally:
        if client is None:
            await active_client.aclose()

    still_missing = await find_missing_daily_instruments(session, trade_date)
    result.still_missing = len(still_missing)
    result.verified = max(result.initially_missing - result.still_missing, 0)
    result.coverage_after = (
        (eligible - result.still_missing) / eligible if eligible else 0.0
    )
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        "[GAP-REPAIR] 完成 trade_date=%s fetched=%d (ths=%d em=%d) inserted=%d "
        "verified=%d still_missing=%d coverage=%.4f elapsed=%.1fs",
        trade_date,
        result.fetched,
        result.ths_fetched,
        result.eastmoney_fetched,
        result.inserted,
        result.verified,
        result.still_missing,
        result.coverage_after,
        result.elapsed_seconds,
    )
    return result


async def compare_db_vs_source_for_date(
    session: AsyncSession,
    trade_date: date,
    *,
    source: str = "ths",
    sh: int = _AB_SAMPLE_SH,
    sz: int = _AB_SAMPLE_SZ,
    bj: int = _AB_SAMPLE_BJ,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ConsistencyReport:
    """只读 A/B：分层抽样比较 DB ``bars_daily`` raw 与指定外部源。

    分层：SH / SZ / BJ 各自等距抽样（北交所不足则全取）。样本全部来自
    「当日在 DB 已有 bar」的活跃 A 股，因此两侧都有可比数据。

    这是**写库前置门禁**：只有本报告通过 :func:`validate_consistency` 才允许写
    目标交易日。

    Args:
        source: ``"ths"``（同花顺不复权，默认）或 ``"eastmoney"``（fqt=0）。

    本函数**不写任何数据**。
    """
    report = ConsistencyReport(trade_date=trade_date)
    report.reference_trade_date = trade_date
    report.source = source

    sampled: list[Instrument] = []
    for market, limit in (("SH", sh), ("SZ", sz), ("BJ", bj)):
        sampled.extend(
            await _evenly_sample_instruments(session, trade_date, market, limit)
        )
    report.sample_requested = len(sampled)

    if not sampled:
        return report

    # 读 DB 侧对照值
    db_rows = (
        await session.execute(
            select(
                BarDaily.instrument_id,
                BarDaily.open,
                BarDaily.high,
                BarDaily.low,
                BarDaily.close,
                BarDaily.volume,
                BarDaily.amount,
            ).where(
                BarDaily.trade_date == trade_date,
                BarDaily.instrument_id.in_([i.id for i in sampled]),
            )
        )
    ).all()
    db_by_id = {row[0]: row for row in db_rows}

    semaphore = asyncio.Semaphore(max(concurrency, 1))
    limits = httpx.Limits(
        max_connections=max(concurrency, 1),
        max_keepalive_connections=max(concurrency, 1),
    )
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0)

    async def fetch_one(inst: Instrument, client: httpx.AsyncClient) -> tuple[Any, Any, Any]:
        async with semaphore:
            if source == "eastmoney":
                return await _exact_bar_from_source(
                    lambda: fetch_eastmoney_daily_kline(
                        client, inst.symbol, inst.market, trade_date, trade_date
                    ),
                    client, inst, trade_date,
                )
            return await _exact_bar_from_source(
                lambda: fetch_ths_raw_daily(client, inst.symbol, trade_date, trade_date),
                client, inst, trade_date,
            )

    ohlc_diffs: list[Decimal] = []
    volume_diffs: list[Decimal] = []
    amount_rels: list[float] = []

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        fetched = await asyncio.gather(
            *(fetch_one(inst, client) for inst in sampled)
        )

    for inst, (record, error) in zip(sampled, fetched, strict=True):
        if record is None:
            report.fetch_failed += 1
            report.failed_symbols.append(inst.symbol)
            if error is not None and len(report.mismatches) < 20:
                report.mismatches.append(f"FETCH-FAIL {inst.symbol}: {error}")
            continue

        report.fetch_succeeded += 1
        db_row = db_by_id.get(inst.id)
        if db_row is None:
            report.missing_in_db += 1
            continue

        _, db_o, db_h, db_l, db_c, db_v, db_a = db_row
        ext_o = _to_decimal(record.get("open"))
        ext_h = _to_decimal(record.get("high"))
        ext_l = _to_decimal(record.get("low"))
        ext_c = _to_decimal(record.get("close"))
        ext_v = _to_decimal(record.get("volume"))
        ext_a = _to_decimal(record.get("amount"))

        if None not in (db_o, db_h, db_l, db_c, ext_o, ext_h, ext_l, ext_c):
            worst = max(
                abs(Decimal(str(db_o)) - ext_o),
                abs(Decimal(str(db_h)) - ext_h),
                abs(Decimal(str(db_l)) - ext_l),
                abs(Decimal(str(db_c)) - ext_c),
            )
            report.ohlc_compared += 1
            ohlc_diffs.append(worst)
            if worst > report.ohlc_max_abs_diff:
                report.ohlc_max_abs_diff = worst
            if worst <= _PRICE_TOLERANCE:
                report.ohlc_exact += 1
            else:
                report.ohlc_bad += 1
                if len(report.mismatches) < 20:
                    report.mismatches.append(
                        f"OHLC {inst.symbol} db=({db_o},{db_h},{db_l},{db_c}) "
                        f"ext=({ext_o},{ext_h},{ext_l},{ext_c}) diff={worst}"
                    )

        if db_v is not None and ext_v is not None:
            vdiff = abs(Decimal(str(db_v)) - ext_v)
            report.volume_compared += 1
            volume_diffs.append(vdiff)
            if vdiff > report.volume_max_abs_diff:
                report.volume_max_abs_diff = vdiff
            if vdiff <= _VOLUME_TOLERANCE:
                report.volume_exact += 1
            else:
                report.volume_bad += 1
                if len(report.mismatches) < 20:
                    report.mismatches.append(
                        f"VOL {inst.symbol} db={db_v} ext={ext_v} diff={vdiff}"
                    )

        if db_a is not None and ext_a is not None:
            db_a_dec = Decimal(str(db_a))
            adiff = abs(db_a_dec - ext_a)
            base = db_a_dec if db_a_dec != 0 else Decimal("1")
            rel = float(adiff / base)
            report.amount_compared += 1
            amount_rels.append(rel)
            if adiff > _AMOUNT_ABS_TOLERANCE and Decimal(str(rel)) > _AMOUNT_REL_TOLERANCE:
                report.amount_bad += 1
                if len(report.mismatches) < 20:
                    report.mismatches.append(
                        f"AMT {inst.symbol} db={db_a} ext={ext_a} "
                        f"abs={adiff} rel={rel:.6f}"
                    )

    if report.ohlc_compared:
        report.ohlc_bad_ratio = report.ohlc_bad / report.ohlc_compared
    if report.volume_compared:
        report.volume_bad_ratio = report.volume_bad / report.volume_compared
    if report.amount_compared:
        report.amount_bad_ratio = report.amount_bad / report.amount_compared
    if amount_rels:
        ordered = sorted(amount_rels)
        report.amount_p50_rel = _percentile(ordered, 0.50)
        report.amount_p95_rel = _percentile(ordered, 0.95)
        report.amount_p99_rel = _percentile(ordered, 0.99)
        report.amount_max_rel = float(ordered[-1])

    logger.info("[AB-CONSISTENCY source=%s] %s", source, report.summary())
    return report


async def compare_db_vs_ths_for_date(
    session: AsyncSession,
    trade_date: date,
    *,
    sh: int = _AB_SAMPLE_SH,
    sz: int = _AB_SAMPLE_SZ,
    bj: int = _AB_SAMPLE_BJ,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ConsistencyReport:
    """只读 A/B：DB ``bars_daily`` raw vs 同花顺**不复权** ``/00/``。"""
    return await compare_db_vs_source_for_date(
        session, trade_date, source="ths",
        sh=sh, sz=sz, bj=bj, concurrency=concurrency,
    )


async def compare_db_vs_eastmoney_for_date(
    session: AsyncSession,
    trade_date: date,
    *,
    sh: int = _AB_SAMPLE_SH,
    sz: int = _AB_SAMPLE_SZ,
    bj: int = _AB_SAMPLE_BJ,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ConsistencyReport:
    """只读 A/B：DB ``bars_daily`` raw vs Eastmoney ``fqt=0``。

    保留用于东财恢复后的交叉验证；当前生产出口通常被东财硬封，
    批量路径请用 :func:`compare_db_vs_ths_for_date`。
    """
    return await compare_db_vs_source_for_date(
        session, trade_date, source="eastmoney",
        sh=sh, sz=sz, bj=bj, concurrency=concurrency,
    )


def validate_consistency(report: ConsistencyReport) -> None:
    """A/B 门禁：坏点比例超过 1% 即抛 :class:`SourceConsistencyError`。

    门禁分两层：
    1. **覆盖率门禁**（本次新增，P0）：抽到的样本里成功抓取的比例必须
       ``>= _MIN_FETCH_SUCCESS_RATIO``，且抓取成功样本中 OHLC / volume / amount
       任一项真正可比的比例必须 ``>= _MIN_COMPARISON_RATIO``。
       否则「200 抽 3 抓到且 3 全一致」「0/200 三个 bad_ratio 全 0」这类
       抓取失败被误判为通过的情况必须被挡住。
    2. **坏点门禁**：可比样本中坏点比例超过 1% 即禁止写入。

    Raises:
        SourceConsistencyError: 覆盖率或坏点比例任一超限。
    """
    problems: list[str] = []

    if report.sample_requested <= 0:
        problems.append("no samples requested")
    else:
        fetch_ratio = report.fetch_succeeded / report.sample_requested
        if fetch_ratio < _MIN_FETCH_SUCCESS_RATIO:
            problems.append(
                f"fetch coverage too low: "
                f"{report.fetch_succeeded}/{report.sample_requested}="
                f"{fetch_ratio:.1%}"
            )

    expected_compared = report.fetch_succeeded

    if expected_compared > 0:
        ohlc_ratio = report.ohlc_compared / expected_compared
        volume_ratio = report.volume_compared / expected_compared
        amount_ratio = report.amount_compared / expected_compared

        if ohlc_ratio < _MIN_COMPARISON_RATIO:
            problems.append(f"OHLC comparison coverage={ohlc_ratio:.1%}")
        if volume_ratio < _MIN_COMPARISON_RATIO:
            problems.append(f"volume comparison coverage={volume_ratio:.1%}")
        if amount_ratio < _MIN_COMPARISON_RATIO:
            problems.append(f"amount comparison coverage={amount_ratio:.1%}")

    if report.ohlc_bad_ratio > _CONSISTENCY_BAD_RATIO_LIMIT:
        problems.append(
            f"price bad_ratio={report.ohlc_bad_ratio:.4f} "
            f"(bad={report.ohlc_bad}/{report.ohlc_compared}, "
            f"max_abs={report.ohlc_max_abs_diff})"
        )
    if report.volume_bad_ratio > _CONSISTENCY_BAD_RATIO_LIMIT:
        problems.append(
            f"volume bad_ratio={report.volume_bad_ratio:.4f} "
            f"(bad={report.volume_bad}/{report.volume_compared}, "
            f"max_abs={report.volume_max_abs_diff})"
        )
    if report.amount_bad_ratio > _CONSISTENCY_BAD_RATIO_LIMIT:
        problems.append(
            f"amount bad_ratio={report.amount_bad_ratio:.4f} "
            f"(bad={report.amount_bad}/{report.amount_compared})"
        )
    if problems:
        raise SourceConsistencyError(
            "SOURCE_CONSISTENCY_GATE_FAILED: " + "; ".join(problems)
        )


async def read_daily_fingerprint(
    session: AsyncSession,
    trade_date: date,
) -> dict[str, object]:
    """只读：某交易日的行数 + close/volume/amount 求和（用于「修复前后不变」验收）。"""
    from sqlalchemy import func

    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(BarDaily.close), 0),
                func.coalesce(func.sum(BarDaily.volume), 0),
                func.coalesce(func.sum(BarDaily.amount), 0),
            ).where(BarDaily.trade_date == trade_date)
        )
    ).one()
    return {
        "trade_date": trade_date.isoformat(),
        "count": int(row[0] or 0),
        "sum_close": str(row[1]),
        "sum_volume": str(row[2]),
        "sum_amount": str(row[3]),
    }


__all__ = [
    "ConsistencyReport",
    "MarketWideRepairResult",
    "SourceConsistencyError",
    "bulk_insert_raw_daily_repair",
    "compare_db_vs_eastmoney_for_date",
    "compare_db_vs_source_for_date",
    "compare_db_vs_ths_for_date",
    "read_daily_fingerprint",
    "repair_market_wide_daily_gap",
    "validate_consistency",
]
