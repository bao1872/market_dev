"""R4: pytdx historical daily volume contract 验证（必须走 production Adapter 路径）。

Phase A：4 台 stable server **各自独立** `PytdxAdapter(servers=[server], max_retries=1)`，
         对**同一组** 40 只（20 SH + 20 SZ，T 日 DB 已有 raw bar）调用 production
         `adapter.get_daily_bars(symbol, T, T)`，验证 2026-09-11 的 OHLC / volume / amount
         与 canonical DB 的一致性。
Phase B：四台 cross-consistency（volume/amount median、OHLC 是否一致）。
Phase C：4-server pool 生产路径 smoke（**另一组** 40 只）。

硬约束：
- 只用 `adapter.get_daily_bars()`，禁止直接调 `adapter.api.get_security_bars`（要证明
  的是生产 Adapter 的解析/分页/字段映射契约）。
- 必须命中**恰好一根** T 日 bar，禁止拿最后一根当 T。
- DB 对照一次批量 SELECT，纯内存比较，禁止逐 symbol 查询。
- 只读网络 + 只读 DB；不修改 `PYTDX_SERVERS` / retry / cache。

用法：
    python scripts/verify_pytdx_daily_contract.py
    python scripts/verify_pytdx_daily_contract.py --output r4.json
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from sqlalchemy import select  # noqa: E402

from app.core.pytdx_adapter import PytdxAdapter  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.models.bar import BarDaily  # noqa: E402
from app.models.instrument import Instrument  # noqa: E402
from app.services.instrument_maintenance_service import stock_symbol_sql_filter  # noqa: E402

T = date(2026, 9, 11)
N_PER_MARKET = 20

STABLE_SERVERS: list[tuple[str, int]] = [
    ("159.75.55.232", 7709),
    ("sztdx.gtjas.com", 7709),
    ("shtdx.gtjas.com", 7709),
    ("jstdx.gtjas.com", 7709),
]

PRICE_TOL = Decimal("0.01")
GATE_FETCH_RATIO = 0.95
GATE_OHLC_BAD_RATIO = 0.01
GATE_OHLC_MAX_ABS = Decimal("0.01")
GATE_VOL_MEDIAN_LO = 0.0095
GATE_VOL_MEDIAN_HI = 0.0105
GATE_AMT_MEDIAN_LO = 0.98
GATE_AMT_MEDIAN_HI = 1.02


def _dec(value: object) -> Decimal | None:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except Exception:  # noqa: BLE001
        return None


def _stats(xs: list[float]) -> tuple[float | None, float | None, float | None]:
    if not xs:
        return None, None, None
    s = sorted(xs)
    return s[0], s[len(s) // 2], s[-1]


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


@dataclass
class ServerReport:
    server: str
    resolved_ip: str | None = None

    requested: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    exact_date_ok: int = 0

    ohlc_compared: int = 0
    ohlc_bad: int = 0
    ohlc_max_abs: Decimal = Decimal("0")

    volume_compared: int = 0
    volume_ratio_min: float | None = None
    volume_ratio_median: float | None = None
    volume_ratio_max: float | None = None

    amount_compared: int = 0
    amount_ratio_min: float | None = None
    amount_ratio_median: float | None = None
    amount_ratio_max: float | None = None

    errors: list[str] = field(default_factory=list)
    gate_pass: bool = False
    gate_failures: list[str] = field(default_factory=list)

    def gate(self) -> None:
        fails: list[str] = []
        if self.requested and self.fetch_ok / self.requested < GATE_FETCH_RATIO:
            fails.append(f"fetch_ok {self.fetch_ok}/{self.requested} < 95%")
        if self.ohlc_compared:
            bad_ratio = self.ohlc_bad / self.ohlc_compared
            if bad_ratio > GATE_OHLC_BAD_RATIO:
                fails.append(f"OHLC bad_ratio={bad_ratio:.4f} > 1%")
        if self.ohlc_max_abs > GATE_OHLC_MAX_ABS:
            fails.append(f"OHLC max_abs={self.ohlc_max_abs} > 0.01")
        med = self.volume_ratio_median
        if med is None or not (GATE_VOL_MEDIAN_LO <= med <= GATE_VOL_MEDIAN_HI):
            fails.append(f"volume median={med} not in [0.0095, 0.0105]")
        amed = self.amount_ratio_median
        if amed is None or not (GATE_AMT_MEDIAN_LO <= amed <= GATE_AMT_MEDIAN_HI):
            fails.append(f"amount median={amed} not in [0.98, 1.02]")
        self.gate_failures = fails
        self.gate_pass = not fails


async def _load_samples(session, *, second_half: bool) -> tuple[list[Instrument], dict[str, tuple]]:
    """确定性等距抽样：每个市场从 [前半 / 后半] 取 N 只（T 日 DB 已有 raw bar）。"""
    picked: list[Instrument] = []
    for market in ("SH", "SZ"):
        rows = list(
            (
                await session.execute(
                    select(Instrument)
                    .where(Instrument.status == "active")
                    .where(Instrument.market == market)
                    .where(stock_symbol_sql_filter(Instrument))
                    .where(
                        Instrument.id.in_(
                            select(BarDaily.instrument_id).where(BarDaily.trade_date == T)
                        )
                    )
                    .order_by(Instrument.symbol)
                )
            ).scalars().all()
        )
        half = len(rows) // 2
        window = rows[half:] if second_half else rows[:half]
        if not window:
            continue
        step = len(window) / N_PER_MARKET
        picked.extend(
            window[min(int(i * step), len(window) - 1)] for i in range(N_PER_MARKET)
        )

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
                BarDaily.trade_date == T,
                BarDaily.instrument_id.in_([i.id for i in picked]),
            )
        )
    ).all()
    by_id = {i.id: i.symbol for i in picked}
    db_by_symbol = {by_id[r[0]]: r for r in db_rows if r[0] in by_id}
    return picked, db_by_symbol


def _compare_one(
    report: ServerReport,
    adapter: PytdxAdapter,
    instrument: Instrument,
    db_row: tuple,
) -> None:
    report.requested += 1
    try:
        df = adapter.get_daily_bars(instrument.symbol, T, T)
    except Exception as exc:  # noqa: BLE001
        report.fetch_failed += 1
        if len(report.errors) < 12:
            report.errors.append(f"{instrument.symbol}:{type(exc).__name__}")
        return
    if df is None or df.empty:
        report.fetch_failed += 1
        if len(report.errors) < 12:
            report.errors.append(f"{instrument.symbol}:empty")
        return

    exact = df[df["datetime"].dt.date == T]
    if len(exact) != 1:
        report.fetch_failed += 1
        if len(report.errors) < 12:
            report.errors.append(f"{instrument.symbol}:exact={len(exact)}")
        return

    report.fetch_ok += 1
    report.exact_date_ok += 1

    row = exact.iloc[0]
    _, d_o, d_h, d_l, d_c, d_v, d_a = db_row

    ext = {k: _dec(row.get(k)) for k in ("open", "high", "low", "close", "volume", "amount")}
    if None not in (ext["open"], ext["high"], ext["low"], ext["close"]) and None not in (
        d_o, d_h, d_l, d_c,
    ):
        worst = max(
            abs(Decimal(str(d_o)) - ext["open"]),
            abs(Decimal(str(d_h)) - ext["high"]),
            abs(Decimal(str(d_l)) - ext["low"]),
            abs(Decimal(str(d_c)) - ext["close"]),
        )
        report.ohlc_compared += 1
        report.ohlc_max_abs = max(report.ohlc_max_abs, worst)
        if worst > PRICE_TOL:
            report.ohlc_bad += 1

    if ext["volume"] is not None and d_v not in (None, 0) and Decimal(str(d_v)) != 0:
        report.volume_compared += 1
        report._vol_ratios = getattr(report, "_vol_ratios", [])
        report._vol_ratios.append(float(ext["volume"] / Decimal(str(d_v))))

    if ext["amount"] is not None and d_a not in (None, 0) and Decimal(str(d_a)) != 0:
        report.amount_compared += 1
        report._amt_ratios = getattr(report, "_amt_ratios", [])
        report._amt_ratios.append(float(ext["amount"] / Decimal(str(d_a))))


def _finalize(report: ServerReport) -> None:
    vmin, vmed, vmax = _stats(getattr(report, "_vol_ratios", []))
    report.volume_ratio_min, report.volume_ratio_median, report.volume_ratio_max = vmin, vmed, vmax
    amin, amed, amax = _stats(getattr(report, "_amt_ratios", []))
    report.amount_ratio_min, report.amount_ratio_median, report.amount_ratio_max = amin, amed, amax
    report.gate()


def _dump(report: ServerReport) -> dict:
    return {
        "server": report.server,
        "resolved_ip": report.resolved_ip,
        "requested": report.requested,
        "fetch_ok": report.fetch_ok,
        "fetch_failed": report.fetch_failed,
        "exact_date_ok": report.exact_date_ok,
        "ohlc_compared": report.ohlc_compared,
        "ohlc_bad": report.ohlc_bad,
        "ohlc_max_abs_diff": str(report.ohlc_max_abs),
        "volume_compared": report.volume_compared,
        "volume_ratio_min": report.volume_ratio_min,
        "volume_ratio_median": report.volume_ratio_median,
        "volume_ratio_max": report.volume_ratio_max,
        "amount_compared": report.amount_compared,
        "amount_ratio_min": report.amount_ratio_min,
        "amount_ratio_median": report.amount_ratio_median,
        "amount_ratio_max": report.amount_ratio_max,
        "gate_pass": report.gate_pass,
        "gate_failures": report.gate_failures,
        "errors": report.errors,
    }


async def _phase_a(session) -> tuple[list[ServerReport], list[Instrument]]:
    instruments, db_by_symbol = await _load_samples(session, second_half=False)
    print(f"[Phase A] samples={len(instruments)} (SH/SZ 各 {N_PER_MARKET})", flush=True)
    reports: list[ServerReport] = []
    for host, port in STABLE_SERVERS:
        label = f"{host}:{port}"
        rep = ServerReport(server=label, resolved_ip=_resolve(host))
        adapter = PytdxAdapter(servers=[(host, port)], max_retries=1, connect_timeout=1.5)
        t0 = time.perf_counter()
        for inst in instruments:
            db_row = db_by_symbol.get(inst.symbol)
            if db_row is None:
                continue
            _compare_one(rep, adapter, inst, db_row)
        adapter.disconnect()
        _finalize(rep)
        reports.append(rep)
        print(
            f"  {label:22} ip={str(rep.resolved_ip):16} fetch_ok={rep.fetch_ok}/{rep.requested} "
            f"exact={rep.exact_date_ok} vol_med={rep.volume_ratio_median} "
            f"amt_med={rep.amount_ratio_median} gate={'PASS' if rep.gate_pass else 'FAIL'} "
            f"({time.perf_counter() - t0:.1f}s)",
            flush=True,
        )
        if not rep.gate_pass:
            print(f"    gate_failures={rep.gate_failures}", flush=True)
        if rep.errors:
            print(f"    errors={rep.errors[:6]}", flush=True)
    return reports, instruments


async def _phase_c(session) -> dict:
    instruments, db_by_symbol = await _load_samples(session, second_half=True)
    print(f"\n[Phase C] pool smoke samples={len(instruments)}", flush=True)
    adapter = PytdxAdapter(servers=STABLE_SERVERS)
    rep = ServerReport(server="pool(4)")
    for inst in instruments:
        db_row = db_by_symbol.get(inst.symbol)
        if db_row is None:
            continue
        _compare_one(rep, adapter, inst, db_row)
    _finalize(rep)
    out = {
        "requested": rep.requested,
        "fetch_ok": rep.fetch_ok,
        "fetch_failed": rep.fetch_failed,
        "connected_server": adapter.connected_server,
        "successful_connect_count": adapter.successful_connect_count,
        "reconnect_count": adapter.reconnect_count,
        "errors": rep.errors,
    }
    adapter.disconnect()
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return out


QUOTE_VOL_LOTS_LO, QUOTE_VOL_LOTS_HI = 0.0095, 0.0105
QUOTE_VOL_SHARES_LO, QUOTE_VOL_SHARES_HI = 0.98, 1.02

CLASS_LOTS = "QUOTE_VOLUME_LOTS"
CLASS_SHARES = "QUOTE_VOLUME_SHARES"
CLASS_UNVERIFIED = "QUOTE_VOLUME_UNVERIFIED"
CLASS_EMPTY = "QUOTE_EMPTY"
CLASS_ERROR = "QUOTE_ERROR"
MULT_LOTS = 100
MULT_SHARES = 1


@dataclass
class QuoteReport:
    server: str
    resolved_ip: str | None = None
    requested: int = 0
    returned: int = 0

    price_compared: int = 0
    price_bad: int = 0
    price_max_abs: Decimal = Decimal("0")

    amount_compared: int = 0
    amount_ratio_min: float | None = None
    amount_ratio_median: float | None = None
    amount_ratio_max: float | None = None

    vol_compared: int = 0
    vol_ratio_min: float | None = None
    vol_ratio_median: float | None = None
    vol_ratio_max: float | None = None

    daily_reference_missing: int = 0
    classification: str = ""
    suggested_multiplier: int | None = None
    fields_present: dict = field(default_factory=dict)
    source_time_samples: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    _vol: list = field(default_factory=list, repr=False)
    _amt: list = field(default_factory=list, repr=False)


def _probe_quotes(host: str, port: int, instruments: list) -> QuoteReport:
    """Phase D+E：同一台上先取 quote，再对同一 symbol 取 exact-date daily 做直接对照。"""
    rep = QuoteReport(server=f"{host}:{port}", resolved_ip=_resolve(host))
    symbols = [i.symbol for i in instruments]
    rep.requested = len(symbols)
    adapter = PytdxAdapter(servers=[(host, port)], max_retries=1, connect_timeout=1.5)
    try:
        rows = adapter.get_security_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"quote:{type(exc).__name__}: {exc}")
        rep.classification = CLASS_ERROR
        adapter.disconnect()
        return rep

    rep.returned = len(rows)
    by_code: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = r.get("code")
        if isinstance(code, str):
            by_code[code] = r
        st = r.get("servertime")
        if st is not None and len(rep.source_time_samples) < 5:
            rep.source_time_samples.append(str(st))
        for key in ("open", "high", "low", "price", "vol", "amount", "last_close", "servertime"):
            if r.get(key) is not None:
                rep.fields_present[key] = rep.fields_present.get(key, 0) + 1

    if rep.returned == 0:
        rep.classification = CLASS_EMPTY
        adapter.disconnect()
        return rep

    for inst in instruments:
        q = by_code.get(inst.symbol)
        if q is None:
            continue
        try:
            df = adapter.get_daily_bars(inst.symbol, T, T)
        except Exception:  # noqa: BLE001
            rep.daily_reference_missing += 1
            continue
        if df is None or df.empty:
            rep.daily_reference_missing += 1
            continue
        exact = df[df["datetime"].dt.date == T]
        if len(exact) != 1:
            rep.daily_reference_missing += 1
            continue
        d = exact.iloc[0]

        qp = {k: _dec(q.get(k)) for k in ("open", "high", "low", "price", "vol", "amount")}
        dp = {k: _dec(d.get(k)) for k in ("open", "high", "low", "close", "volume", "amount")}

        if None not in (
            qp["open"], qp["high"], qp["low"], qp["price"],
            dp["open"], dp["high"], dp["low"], dp["close"],
        ):
            worst = max(
                abs(qp["open"] - dp["open"]),
                abs(qp["high"] - dp["high"]),
                abs(qp["low"] - dp["low"]),
                abs(qp["price"] - dp["close"]),
            )
            rep.price_compared += 1
            rep.price_max_abs = max(rep.price_max_abs, worst)
            if worst > PRICE_TOL:
                rep.price_bad += 1

        if qp["amount"] is not None and dp["amount"] not in (None, Decimal("0")):
            rep.amount_compared += 1
            rep._amt.append(float(qp["amount"] / dp["amount"]))

        if qp["vol"] is not None and dp["volume"] not in (None, Decimal("0")):
            rep.vol_compared += 1
            rep._vol.append(float(qp["vol"] / dp["volume"]))

    adapter.disconnect()
    rep.amount_ratio_min, rep.amount_ratio_median, rep.amount_ratio_max = _stats(rep._amt)
    rep.vol_ratio_min, rep.vol_ratio_median, rep.vol_ratio_max = _stats(rep._vol)

    med = rep.vol_ratio_median
    if med is None:
        rep.classification = CLASS_UNVERIFIED
    elif QUOTE_VOL_LOTS_LO <= med <= QUOTE_VOL_LOTS_HI:
        rep.classification = CLASS_LOTS
        rep.suggested_multiplier = MULT_LOTS
    elif QUOTE_VOL_SHARES_LO <= med <= QUOTE_VOL_SHARES_HI:
        rep.classification = CLASS_SHARES
        rep.suggested_multiplier = MULT_SHARES
    else:
        rep.classification = CLASS_UNVERIFIED
    return rep


def _quote_dump(rep: QuoteReport) -> dict:
    return {
        "server": rep.server,
        "resolved_ip": rep.resolved_ip,
        "requested": rep.requested,
        "quote_returned": rep.returned,
        "price_compared": rep.price_compared,
        "price_bad": rep.price_bad,
        "price_max_abs_diff": str(rep.price_max_abs),
        "amount_compared": rep.amount_compared,
        "amount_ratio_min": rep.amount_ratio_min,
        "amount_ratio_median": rep.amount_ratio_median,
        "amount_ratio_max": rep.amount_ratio_max,
        "volume_compared": rep.vol_compared,
        "quote_to_daily_volume_ratio_min": rep.vol_ratio_min,
        "quote_to_daily_volume_ratio_median": rep.vol_ratio_median,
        "quote_to_daily_volume_ratio_max": rep.vol_ratio_max,
        "daily_reference_missing": rep.daily_reference_missing,
        "classification": rep.classification,
        "suggested_multiplier": rep.suggested_multiplier,
        "fields_present": rep.fields_present,
        "source_time_samples": rep.source_time_samples,
        "errors": rep.errors,
    }


async def _phase_d(session) -> list[QuoteReport]:
    instruments, _ = await _load_samples(session, second_half=False)
    print(f"\n[Phase D/E] quote sweep+proof samples={len(instruments)}", flush=True)
    reports: list[QuoteReport] = []
    for host, port in STABLE_SERVERS:
        rep = _probe_quotes(host, port, instruments)
        reports.append(rep)
        print(
            f"  {rep.server:22} returned={rep.returned}/{rep.requested} "
            f"price_bad={rep.price_bad}/{rep.price_compared} "
            f"vol_med={rep.vol_ratio_median} amt_med={rep.amount_ratio_median} "
            f"-> {rep.classification} (x{rep.suggested_multiplier})",
            flush=True,
        )
        if rep.errors:
            print(f"    errors={rep.errors[:4]}", flush=True)
    return reports


async def _amain(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        reports, _ = await _phase_a(session)
        phase_c = await _phase_c(session)
        quote_reports = await _phase_d(session)

    pass_servers = [r.server for r in reports if r.gate_pass]
    vol_medians = [r.volume_ratio_median for r in reports if r.volume_ratio_median is not None]
    amt_medians = [r.amount_ratio_median for r in reports if r.amount_ratio_median is not None]

    print("\n=== Phase B: cross-server consistency ===")
    print(f"volume medians: {[round(v, 6) for v in vol_medians]}")
    print(f"amount medians: {[round(a, 6) for a in amt_medians]}")
    print(f"PASS servers ({len(pass_servers)}): {pass_servers}")

    classifications = [r.classification for r in quote_reports]
    print("\n=== Phase D/E: quote contract ===")
    print(f"classifications: {classifications}")
    print(f"quote_returned: {[r.returned for r in quote_reports]}")

    payload = {
        "phase_a": [_dump(r) for r in reports],
        "phase_b": {
            "pass_servers": pass_servers,
            "pass_count": len(pass_servers),
            "volume_medians": vol_medians,
            "amount_medians": amt_medians,
            "volume_median_spread": (
                max(vol_medians) - min(vol_medians) if vol_medians else None
            ),
        },
        "phase_c": phase_c,
        "phase_d_quote": [_quote_dump(r) for r in quote_reports],
        "phase_e_quote_classifications": classifications,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if len(pass_servers) >= 2 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R4 pytdx daily volume contract verification")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args(argv)
    import asyncio

    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
