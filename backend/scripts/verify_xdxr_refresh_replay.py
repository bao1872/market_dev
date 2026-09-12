"""G1B-3B2 historical replay 验证器（R3）。

目的：在**已恢复的**历史交易日上证明 refresh-set 不漏任何「实际发生变化」的标的。

方法（ground truth 与 planner 输入严格分离）：
    1. 对 SH/SZ active universe，每只股票**只取一次** XDXR（``get_xdxr_info``）。
    2. 同一份 XDXR 分别算 ``fingerprint(as_of=T-1)`` 与 ``fingerprint(as_of=T)``
       → ``actual_changed_symbols``（真正发生已生效公司行为变化的标的）。
    3. 用 **T-1 视角**构造 schedule state（``scanned_as_of=T-1`` +
       ``next_future_corporate_action_date(as_of=T-1)``），即 T-1 盘后写入 Redis 的内容。
    4. 用 T-1 的 DB raw close + T 日「交易所除权参考价」重建 previous_close 信号：
       ``参考价 = (prev_close - fenhong/10 + peigujia*peigu/10) / (1 + songzhuangu/10 + peigu/10)``
       （无当日 category=1 事件时 参考价 ≡ prev_close → NO_ACTION_SIGNAL）。
       说明：T 日 EOD snapshot 不存在（T 日盘后未运行），故这里按交易所规则**重建**
       该信号，而不是读取真实 snapshot 字段；重建只依赖交易所公开规则与 XDXR 自身。
    5. 用生产 planner（``plan_xdxr_refresh``，与 scheduler 同一纯函数）算 refresh-set。
    6. 硬门禁：``missed = actual_changed - planned_refresh`` 必须为空。

用法：
    python -m scripts.verify_xdxr_refresh_replay --t 2026-09-11 --t-1 2026-09-10
    python -m scripts.verify_xdxr_refresh_replay --t 2026-09-11 --t-1 2026-09-10 --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

# 本地执行时载入 backend/.env（容器内无该文件 → no-op）。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select  # noqa: E402

from app.core.pytdx_adapter import get_pytdx_adapter  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.models.bar import BarDaily  # noqa: E402
from app.models.calendar import TradingCalendar  # noqa: E402
from app.models.instrument import Instrument  # noqa: E402
from app.services.adjustment_factor_calculator import (  # noqa: E402
    corporate_action_fingerprint,
    next_future_corporate_action_date,
)
from app.services.adjustment_factor_service import (  # noqa: E402
    CorporateActionScheduleState,
)
from app.services.instrument_maintenance_service import stock_symbol_sql_filter  # noqa: E402
from app.services.xdxr_refresh_planner import (  # noqa: E402
    classify_previous_close_signal,
    plan_xdxr_refresh,
)

logger = logging.getLogger("xdxr_replay")

_SH_SZ = ("SH", "SZ")
_ROTATION_SIZE = 3


def _exchange_prev_close(
    prior_raw_close: Decimal | None,
    *,
    fenhong: Decimal,
    songzhuangu: Decimal,
    peigu: Decimal,
    peigujia: Decimal,
) -> Decimal | None:
    """交易所除权除息参考价（T 日「前收盘」），无事件时恒等于 prior_raw_close。"""
    if prior_raw_close is None:
        return None
    denom = Decimal("1") + songzhuangu / Decimal("10") + peigu / Decimal("10")
    if denom == 0:
        return prior_raw_close
    return (prior_raw_close - fenhong / Decimal("10") + peigujia * peigu / Decimal("10")) / denom


def _day_events(frame, trade_date: date) -> list[dict]:
    """取 ``date == trade_date`` 且 ``category == 1``（除权除息）的事件。"""
    if frame is None or frame.empty:
        return []
    out: list[dict] = []
    for _, row in frame.iterrows():
        cat = row.get("category")
        try:
            if int(cat) != 1:
                continue
        except (TypeError, ValueError):
            continue
        stamp = row.get("date")
        if stamp is None:
            continue
        try:
            event_date = date.fromisoformat(str(stamp)[:10])
        except ValueError:
            continue
        if event_date != trade_date:
            continue
        out.append(row.to_dict())
    return out


def _dec(value: object) -> Decimal:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else Decimal("0")
    except Exception:  # noqa: BLE001
        return Decimal("0")


async def _load_universe(session, limit: int | None) -> list[Instrument]:
    stmt = (
        select(Instrument)
        .where(Instrument.status == "active")
        .where(Instrument.market.in_(_SH_SZ))
        .where(stock_symbol_sql_filter(Instrument))
        .order_by(Instrument.symbol)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def _load_prior_close(session, instruments: list[Instrument], t1: date) -> dict[str, Decimal]:
    rows = (
        await session.execute(
            select(BarDaily.instrument_id, BarDaily.close).where(
                BarDaily.trade_date == t1,
                BarDaily.instrument_id.in_([i.id for i in instruments]),
            )
        )
    ).all()
    by_id = {i.id: i.symbol for i in instruments}
    return {by_id[iid]: close for iid, close in rows if iid in by_id}


async def _trade_day_coordinates(session, t: date, t1: date) -> tuple[int | None, int | None]:
    """返回 (trade_day_ordinal(T), schedule_age(T-1 -> T))，唯一事实源 TradingCalendar。"""
    dates = list(
        (
            await session.scalars(
                select(TradingCalendar.trade_date)
                .where(TradingCalendar.market == "A")
                .where(TradingCalendar.is_trading_day.is_(True))
                .where(TradingCalendar.trade_date <= t)
                .order_by(TradingCalendar.trade_date.asc())
            )
        ).all()
    )
    idx = {d: i for i, d in enumerate(dates)}
    ordinal = idx.get(t)
    age = None
    if ordinal is not None and t1 in idx:
        age = idx[t] - idx[t1]
    return ordinal, age


async def _run(args: argparse.Namespace) -> int:
    adapter = get_pytdx_adapter()
    async with AsyncSessionLocal() as session:
        instruments = await _load_universe(session, args.limit)
        prior_close = await _load_prior_close(session, instruments, args.t1)
        trade_day_ordinal, schedule_age = await _trade_day_coordinates(session, args.t, args.t1)

    print(
        f"universe(SH/SZ active)={len(instruments)} T={args.t} T-1={args.t1} "
        f"trade_day_ordinal={trade_day_ordinal} schedule_age={schedule_age}",
        flush=True,
    )

    actual_changed: list[str] = []
    planned_refresh: list[str] = []
    reason_counts: dict[str, int] = {}
    per_changed_reasons: dict[str, list[str]] = {}
    xdxr_errors: list[str] = []
    fp_prev_unknown: list[str] = []

    for pos, inst in enumerate(instruments, 1):
        symbol = inst.symbol
        try:
            frame = adapter.get_xdxr_info(symbol, force_refresh=True)
        except Exception as exc:  # noqa: BLE001
            xdxr_errors.append(f"{symbol}:{type(exc).__name__}")
            continue

        fp_prev, _ = corporate_action_fingerprint(frame, effective_as_of=args.t1)
        fp_t, _ = corporate_action_fingerprint(frame, effective_as_of=args.t)
        if fp_prev != fp_t:
            actual_changed.append(symbol)

        # T-1 盘后写入的 schedule state
        schedule = CorporateActionScheduleState(
            scanned_as_of=args.t1,
            next_event_date=next_future_corporate_action_date(frame, effective_as_of=args.t1),
        )

        # 重建 T 日 previous_close 信号
        raw_prev = prior_close.get(symbol)
        events = _day_events(frame, args.t)
        if events:
            ev = events[0]
            snapped = _exchange_prev_close(
                raw_prev,
                fenhong=_dec(ev.get("fenhong")),
                songzhuangu=_dec(ev.get("songzhuangu")),
                peigu=_dec(ev.get("peigu")),
                peigujia=_dec(ev.get("peigujia")),
            )
        else:
            snapped = raw_prev
        if raw_prev is None:
            fp_prev_unknown.append(symbol)
        signal = classify_previous_close_signal(
            snapshot_previous_close=snapped, prior_raw_close=raw_prev
        )

        decision = plan_xdxr_refresh(
            symbol=symbol,
            trade_date=args.t,
            trade_day_ordinal=trade_day_ordinal,
            schedule=schedule,
            schedule_age_trade_days=schedule_age,
            previous_close_signal=signal,
            rotation_size=_ROTATION_SIZE,
        )
        for reason in decision.reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if decision.refresh:
            planned_refresh.append(symbol)
        if fp_prev != fp_t:
            per_changed_reasons[symbol] = list(decision.reasons)

        if pos % 200 == 0:
            print(
                f"  ... {pos}/{len(instruments)} actual_changed={len(actual_changed)} "
                f"planned={len(planned_refresh)}",
                flush=True,
            )

    missed = sorted(set(actual_changed) - set(planned_refresh))
    universe = len(instruments)
    report = {
        "t": args.t.isoformat(),
        "t_minus_1": args.t1.isoformat(),
        "universe_count": universe,
        "xdxr_error_count": len(xdxr_errors),
        "xdxr_error_sample": xdxr_errors[:20],
        "prior_close_missing_count": len(fp_prev_unknown),
        "actual_changed_count": len(actual_changed),
        "actual_changed_symbols": sorted(actual_changed),
        "refresh_count": len(planned_refresh),
        "refresh_ratio": (len(planned_refresh) / universe) if universe else 0.0,
        "missed_count": len(missed),
        "missed_symbols": missed,
        "reason_counts": dict(sorted(reason_counts.items())),
        "changed_symbol_reasons": per_changed_reasons,
        "trade_day_ordinal": trade_day_ordinal,
        "schedule_age": schedule_age,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not missed else 1


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="G1B-3B2 XDXR refresh-set historical replay")
    p.add_argument("--t", type=_parse_date, required=True, help="replay 目标交易日 T")
    p.add_argument("--t-1", type=_parse_date, required=True, dest="t1", help="T 的前一交易日")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 只（smoke 用）")
    p.add_argument("--output", type=str, default=None, help="报告 JSON 输出路径")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
