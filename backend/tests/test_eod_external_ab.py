"""[EOD-SNAPSHOT] 外部数据源 A/B 一致性测试（真实网络；标记 external_data）。

目的（对应用户要求「不要只因为两边都声称不复权就宣布一致」）：
1. 价格刻度：东方财富快照 ``f18``（昨收）必须等于其自身 ``fqt=0`` 日线在前一交易日的收盘。
   这直接钉死「fltt=2 下价格已是元、不得 ÷100」这一契约。
2. 单位：``f5``=手、``f6``=元。用内部恒等式 ``f6 ≈ f5 × 100 × f2`` 验证
   （若 f5 是「股」或 f6 是「万元」，该式会相差 100/10000 倍）。
3. 跨源：东方财富 ``fqt=0`` 日线 vs pytdx raw 日线，在**同一交易日**比较 OHLCV，
   输出绝对误差与相对误差，不低于 30 只样本。

诚实性约束：
- 外部网络失败 **不得** 伪装成通过，也不得静默 skip —— 一律 ``pytest.fail`` 并给出原因
  （尤其是 TDX 源不可用时，必须显式失败并说明，而不是「跳过」）。
- mismatch 必须打印明细，不得吞掉。

样本覆盖：沪主板 / 深主板 / 创业板 / 科创板 / ST / 高成交量 / 低成交量。
已知局限：样本不保证命中「除权日附近」，该维度未覆盖（不假装已覆盖）。
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import eod_market_snapshot_provider as prov

pytestmark = [pytest.mark.external_data]

_SH = ZoneInfo("Asia/Shanghai")
_PRICE_TOL = 0.01
_MIN_SAMPLE = 30
_BOARDS = ("m:1+t:2", "m:1+t:23", "m:0+t:6", "m:0+t:80")
_TDX_MIN_USABLE_PRICE = 0.01


def _fetch_board(client: httpx.Client, fs: str, pages: int = 1) -> list[dict[str, Any]]:
    """按板块拉取快照原始行（逐主机尝试；失败抛断言，不静默）。"""
    last: str = "unknown"
    for host in prov.EASTMONEY_CLIST_HOSTS:
        try:
            out: list[dict[str, Any]] = []
            for pn in range(1, pages + 1):
                resp = client.get(
                    f"https://{host}{prov._CLIST_PATH}",  # noqa: SLF001
                    params={
                        "pn": pn,
                        "pz": prov.DEFAULT_PAGE_SIZE,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f12",
                        "fs": fs,
                        "fields": prov.FIELDS,
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                out.extend(data.get("diff") or [])
            return out
        except Exception as exc:  # noqa: BLE001 - 逐主机兜底
            last = f"{host}: {type(exc).__name__} {exc}"
            continue
    pytest.fail(f"东方财富全市场快照不可达（所有候选主机失败）：{last}")


def _collect_sample() -> list[prov.EodSnapshotRow]:
    with httpx.Client(timeout=20.0) as client:
        raw: list[dict[str, Any]] = []
        for fs in _BOARDS:
            raw.extend(_fetch_board(client, fs))

    rows = prov.normalize_snapshot_rows(raw)
    assert rows, "快照归一化后为空 —— 无法进行 A/B"

    def pick(pred, limit: int) -> list[prov.EodSnapshotRow]:
        return [r for r in rows if pred(r)][:limit]

    selected: list[prov.EodSnapshotRow] = []
    selected += pick(lambda r: r.symbol.startswith(("600", "601", "603", "605")), 8)
    selected += pick(lambda r: r.symbol.startswith(("000", "001", "003")), 6)
    selected += pick(lambda r: r.symbol.startswith(("300", "301")), 6)
    selected += pick(lambda r: r.symbol.startswith("688"), 6)
    selected += pick(lambda r: "ST" in r.name.upper(), 4)

    tradable = [r for r in rows if (r.volume or 0) > 0 and (r.close or 0) > 0]
    by_vol = sorted(tradable, key=lambda r: r.volume or 0)
    selected += by_vol[:3]
    selected += by_vol[-3:]

    # 去重并保序
    seen: set[str] = set()
    out: list[prov.EodSnapshotRow] = []
    for r in selected:
        if r.symbol in seen:
            continue
        seen.add(r.symbol)
        out.append(r)
    return out


def _kline(client: httpx.Client, symbol: str, market: str, beg: date, end: date) -> list[list[str]]:
    params = {
        "secid": prov._eastmoney_secid(symbol, market),  # noqa: SLF001
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "beg": beg.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "rtntype": "6",
        "klt": "101",
        "fqt": "0",
    }
    last: str = "unknown"
    for host in prov.EASTMONEY_HIS_HOSTS:
        try:
            resp = client.get(f"https://{host}{prov._KLINE_PATH}", params=params, timeout=20.0)  # noqa: SLF001
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            klines = [line.split(",") for line in (data.get("klines") or [])]
            if klines:
                return klines
            # 主机可达但该路径无数据（例如部分镜像不提供历史 K 线）→ 继续下一个主机
            last = f"{host}: 返回空 klines"
        except Exception as exc:  # noqa: BLE001
            last = f"{host}: {type(exc).__name__} {exc}"
            continue
    pytest.fail(f"东方财富历史 K 线不可达 {symbol}: {last}")


def test_snapshot_previous_close_matches_raw_kline_close() -> None:
    """f18（昨收）必须等于 fqt=0 日线中「快照日之前最后一个交易日」的收盘。

    这条断言同时锁死三件事：①候选主机可达；②secid/市场映射正确；
    ③价格刻度为元（若被误 ÷100，误差将是 100 倍量级）。
    """
    sample = _collect_sample()
    assert len(sample) >= _MIN_SAMPLE, f"样本不足 {_MIN_SAMPLE}：{len(sample)}"

    trade_date = max((r.trade_date for r in sample if r.trade_date), default=None)
    assert trade_date is not None, "快照未提供可用 f124 交易日"

    abs_diffs: list[float] = []
    rel_diffs: list[float] = []
    mismatches: list[str] = []
    skipped: list[str] = []

    with httpx.Client(timeout=20.0) as client:
        for row in sample:
            if row.previous_close is None:
                skipped.append(f"{row.symbol}: 无 f18")
                continue
            klines = _kline(
                client, row.symbol, row.market, trade_date - timedelta(days=15), trade_date
            )
            prior = [k for k in klines if date.fromisoformat(k[0]) < trade_date]
            if not prior:
                skipped.append(f"{row.symbol}: 无更早日线")
                continue
            close = float(prior[-1][2])  # f53 close
            if close <= 0:
                skipped.append(f"{row.symbol}: kline close 非正")
                continue
            diff = abs(float(row.previous_close) - close)
            abs_diffs.append(diff)
            rel_diffs.append(diff / close)
            if diff > _PRICE_TOL:
                mismatches.append(
                    f"{row.symbol} f18={row.previous_close} kline_close={close} abs={diff:.4f}"
                )

    print(
        f"\n[AB-price] n={len(abs_diffs)} skipped={len(skipped)} mismatches={len(mismatches)}"
        f" abs_max={max(abs_diffs, default=0):.4f}"
        f" abs_mean={statistics.mean(abs_diffs) if abs_diffs else 0:.6f}"
        f" rel_max={max(rel_diffs, default=0):.6f}"
    )
    for line in mismatches[:10]:
        print(f"[AB-price] MISMATCH {line}")
    for line in skipped[:5]:
        print(f"[AB-price] SKIP {line}")

    assert len(abs_diffs) >= _MIN_SAMPLE, f"有效比较样本不足 {_MIN_SAMPLE}：{len(abs_diffs)}"
    assert not mismatches, f"价格不一致 {len(mismatches)}/{len(abs_diffs)} 条：{mismatches[:5]}"


def test_snapshot_volume_and_amount_units_are_shares_and_yuan() -> None:
    """用恒等式 ``amount ≈ volume × close`` 验证 canonical ``EodSnapshotRow.volume``=股、amount=元。

    单位事实（G1B-2A，2026-09-12 direct 对照证明，见 CHANGE-20260912-001）：
    - canonical ``volume``（``EodSnapshotRow`` / ``bars_daily``）= **股**；
    - pytdx **quote** ``vol`` = **手**（×100 才是股）——那发生在 quote→canonical 转换，
      **不是** 本测试的 canonical 快照。

    差异来自盘中「价格采样时刻」与「成交额累计时刻」不同，属正常时间偏斜；
    单位若错（手 / 万元）则会整体相差 100 或 10000 倍。
    """
    sample = _collect_sample()
    ratios: list[float] = []
    offenders: list[str] = []

    for row in sample:
        if not row.volume or not row.amount or not row.close:
            continue
        if row.volume <= 0 or row.close <= 0:
            continue
        expected = float(row.volume) * float(row.close)
        if expected <= 0:
            continue
        ratio = float(row.amount) / expected
        ratios.append(ratio)
        if not (0.9 <= ratio <= 1.1):
            offenders.append(
                f"{row.symbol} vol={row.volume} price={row.close} amount={row.amount} ratio={ratio:.4f}"
            )

    assert ratios, "无法构造单位一致性样本"

    median = statistics.median(ratios)
    print(
        f"\n[AB-units] n={len(ratios)} ratio_median={median:.4f}"
        f" ratio_min={min(ratios):.4f} ratio_max={max(ratios):.4f}"
        f" offenders={len(offenders)}"
    )
    for line in offenders[:10]:
        print(f"[AB-units] OFFENDER {line}")

    # canonical 单位必须是「股 × 元」：中位比接近 1；差 100 倍即单位判断错误。
    assert 0.95 <= median <= 1.05, (
        f"volume/amount 单位假设不成立：median ratio={median:.4f}（期望≈1.0；"
        "≈0.01 表示 volume 被当成手（多乘了 100），≈100 表示 amount 单位不符）"
    )
    assert len(offenders) / len(ratios) <= 0.2, (
        f"单位一致性超差样本过多：{len(offenders)}/{len(ratios)}"
    )


# =========================================================================
# 跨源：东方财富 raw 日线 vs pytdx raw 日线
# =========================================================================


def _tdx_connect():
    """用 **生产** ``PytdxAdapter`` 取一个可用连接（不再维护第二套 hardcoded server list）。

    生产 server 选择（capability-aware + cooldown）才是被验证的对象；
    测试自己维护 IP 列表会掩盖 pool 腐化（CHANGE-20260912-001）。

    Returns:
        ``(adapter, server_label)``；源不可用时抛 ``PytdxSourceError``（调用方显式失败）。
    """
    from app.core.pytdx_adapter import connect_pytdx

    adapter = connect_pytdx()
    entered = adapter.__enter__()
    return entered, str(entered.connected_server)


def test_eastmoney_raw_kline_vs_pytdx_raw_daily_ohlcv() -> None:
    """东方财富 fqt=0 日线 vs pytdx raw 日线，同交易日 OHLCV 逐项比较。

    TDX 源不可用时**显式失败**（不 skip、不静默），因为那意味着跨源校验本轮未完成。
    """
    sample = _collect_sample()
    trade_date = max((r.trade_date for r in sample if r.trade_date), default=None)
    assert trade_date is not None

    adapter, tdx_host = _tdx_connect()

    market_code = {"SH": 1, "SZ": 0}
    checked = 0
    price_mismatch: list[str] = []
    vol_ratio: list[float] = []
    amt_ratio: list[float] = []

    try:
        with httpx.Client(timeout=20.0) as client:
            for row in sample:
                if row.market not in market_code:
                    continue
                if row.trade_date != trade_date:
                    continue
                klines = _kline(
                    client, row.symbol, row.market, trade_date, trade_date
                )
                kline_today = [k for k in klines if date.fromisoformat(k[0]) == trade_date]
                if not kline_today:
                    continue
                em = kline_today[-1]

                bars = adapter.get_daily_bars(row.symbol, trade_date, trade_date)
                if bars is None or bars.empty:
                    continue
                bars_today = bars[bars["datetime"].dt.date == trade_date]
                if len(bars_today) != 1:
                    continue
                tdx = bars_today.iloc[0].to_dict()
                tdx_day = trade_date.isoformat()
                if tdx_day != trade_date.isoformat():
                    continue

                checked += 1
                for label, em_v, tdx_v in (
                    ("open", em[1], tdx.get("open")),
                    ("high", em[3], tdx.get("high")),
                    ("low", em[4], tdx.get("low")),
                    ("close", em[2], tdx.get("close")),
                ):
                    if tdx_v is None or float(tdx_v) <= 0:
                        continue
                    if abs(float(em_v) - float(tdx_v)) > _PRICE_TOL:
                        price_mismatch.append(
                            f"{row.symbol}.{label} em={em_v} tdx={tdx_v}"
                        )
                em_vol, tdx_vol = float(em[5]), float(tdx.get("vol") or 0)
                em_amt, tdx_amt = float(em[6]), float(tdx.get("amount") or 0)
                if tdx_vol > 0:
                    vol_ratio.append(em_vol / tdx_vol)
                if tdx_amt > 0:
                    amt_ratio.append(em_amt / tdx_amt)
    finally:
        try:
            adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass

    print(
        f"\n[AB-cross] tdx_host={tdx_host} trade_date={trade_date} checked={checked}"
        f" price_mismatch={len(price_mismatch)}"
        f" vol_ratio_median={statistics.median(vol_ratio) if vol_ratio else float('nan'):.4f}"
        f" amt_ratio_median={statistics.median(amt_ratio) if amt_ratio else float('nan'):.4f}"
    )
    for line in price_mismatch[:10]:
        print(f"[AB-cross] PRICE MISMATCH {line}")

    assert checked >= _MIN_SAMPLE, f"跨源有效比较样本不足 {_MIN_SAMPLE}：{checked}"
    assert not price_mismatch, f"跨源价格不一致 {len(price_mismatch)} 条：{price_mismatch[:5]}"
    if vol_ratio:
        assert 0.95 <= statistics.median(vol_ratio) <= 1.05, (
            f"volume 单位不一致：median(EM/TDX)={statistics.median(vol_ratio):.4f}"
        )
    if amt_ratio:
        assert 0.95 <= statistics.median(amt_ratio) <= 1.05, (
            f"amount 单位不一致：median(EM/TDX)={statistics.median(amt_ratio):.4f}"
        )


# =========================================================================
# 北交所 secid 真实性（0.920xxx，不是 2.920xxx）
# =========================================================================

_BJ_FILTER = "m:0+t:81+s:2048"


# =========================================================================
# pytdx 实时 quote → canonical EOD（G1B-2A 外部证明 quote raw_volume 倍率）
# =========================================================================


def test_pytdx_quote_vs_canonical_eod_snapshot() -> None:
    """pytdx 实时 quote 的 OHLCV/amount 与 Eastmoney canonical EOD 对比。

    目的（G1B-2A 必须先钉死再写转换）：
    - quote OHLC/amount 与 canonical EOD 同刻度（元/元），日终可直接取终值；
    - quote ``raw_volume``（手）到 canonical ``volume``（股）的倍率 ≈ 0.01，
      即 canonical = raw_volume × 100。

    pytdx 源不可用 → 显式失败（不 skip），因为本轮目的就是确认真实 provider。
    """
    from app.core.pytdx_adapter import connect_pytdx
    from app.models.instrument import Instrument
    from app.services.pytdx_eod_snapshot_provider import (
        PytdxEodSnapshotError,
        fetch_pytdx_eod_snapshot,
    )

    sample = _collect_sample()
    assert len(sample) >= _MIN_SAMPLE, f"样本不足 {_MIN_SAMPLE}：{len(sample)}"

    trade_date = max((r.trade_date for r in sample if r.trade_date), default=None)
    assert trade_date is not None, "快照未提供可用 trade_date"

    instruments = [
        Instrument(symbol=r.symbol, name=r.name, market=r.market) for r in sample
    ]

    try:
        with connect_pytdx() as adapter:
            snap = fetch_pytdx_eod_snapshot(
                adapter, instruments, trade_date=trade_date, batch_interval_seconds=0.0
            )
    except (PytdxEodSnapshotError, RuntimeError) as exc:
        pytest.fail(
            f"pytdx 源不可用：{type(exc).__name__}: {exc}；"
            "本次 quote→canonical EOD 校验未完成（不得视为通过）"
        )

    quote_by_symbol = {q.symbol: q for q in snap.rows}

    if snap.returned_count == 0:
        pytest.fail(
            f"pytdx quote feed 返回 0 行（source 已连接但无实时行情数据，"
            f"可能处于非交易时段/周末）：requested={snap.requested_count}；"
            "quote→canonical 校验无法取得样本（不得视为通过）"
        )

    price_mismatch: list[str] = []
    price_abs_max: dict[str, float] = dict.fromkeys(("open", "high", "low", "close"), 0.0)
    vol_ratios: list[float] = []
    amt_ratios: list[float] = []
    source_times: list[str] = []
    checked = 0

    for ref in sample:
        if ref.market not in ("SH", "SZ"):
            continue
        q = quote_by_symbol.get(ref.symbol)
        if q is None:
            continue
        checked += 1
        for fld in ("open", "high", "low", "close"):
            qv = getattr(q, fld)
            rv = getattr(ref, fld)
            if qv is None or rv is None:
                continue
            diff = abs(float(qv) - float(rv))
            price_abs_max[fld] = max(price_abs_max[fld], diff)
            if diff > _PRICE_TOL:
                price_mismatch.append(
                    f"{ref.symbol}.{fld} q={qv} ref={rv} abs={diff:.4f}"
                )
        if q.raw_volume is not None and ref.volume:
            vol_ratios.append(float(q.raw_volume) / float(ref.volume))
        if q.amount is not None and ref.amount:
            amt_ratios.append(float(q.amount) / float(ref.amount))
        if q.source_time:
            source_times.append(q.source_time)

    vol_median = statistics.median(vol_ratios) if vol_ratios else float("nan")
    vol_min = min(vol_ratios) if vol_ratios else float("nan")
    vol_max = max(vol_ratios) if vol_ratios else float("nan")
    amt_median = statistics.median(amt_ratios) if amt_ratios else float("nan")

    print(
        f"\n[AB-pytdx-quote] trade_date={trade_date} requested={snap.requested_count}"
        f" returned={snap.returned_count} checked={checked}"
        f" price_mismatch={len(price_mismatch)}"
        f" open_abs_max={price_abs_max['open']:.4f} high_abs_max={price_abs_max['high']:.4f}"
        f" low_abs_max={price_abs_max['low']:.4f} close_abs_max={price_abs_max['close']:.4f}"
        f" vol_ratio_median={vol_median:.5f} vol_ratio_min={vol_min:.5f} vol_ratio_max={vol_max:.5f}"
        f" amt_ratio_median={amt_median:.4f}"
        f" source_time_sample={sorted(set(source_times))[:5]}"
    )
    for line in price_mismatch[:10]:
        print(f"[AB-pytdx-quote] PRICE MISMATCH {line}")

    assert checked >= _MIN_SAMPLE, f"quote→canonical 有效比较样本不足 {_MIN_SAMPLE}：{checked}"
    assert not price_mismatch, f"OHLC 不一致 {len(price_mismatch)} 条：{price_mismatch[:5]}"

    assert 0.0095 <= vol_median <= 0.0105, (
        f"volume 单位不符 ×100 假设：median(raw/canonical)={vol_median:.5f}"
        f"（期望≈0.01；若≈1 表示 pytdx quote 已是股，无需 ×100）"
    )
    assert 0.98 <= amt_median <= 1.02, (
        f"amount 单位不符：median(q/ref)={amt_median:.4f}（期望≈1.0）"
    )


def test_bj_secid_zero_prefix_returns_real_history() -> None:
    """用真实北交所股票验证 secid = ``0.920xxx`` 能取到 fqt=0 历史日线。

    单元测试只证明字符串拼装正确；这里才证明「东财统一行情确实用市场 0」。
    若真实网络不可达则显式失败（不得把单元测试当成 BJ 已验证）。
    """
    with httpx.Client(timeout=20.0) as client:
        raw = _fetch_board(client, _BJ_FILTER, pages=1)

    rows = [r for r in prov.normalize_snapshot_rows(raw) if r.market == "BJ"]
    assert rows, "北交所快照为空 —— 无法验证 BJ secid"

    # 优先真实 920xxx（新代码段），否则退回任意北交所股票
    cand = next((r for r in rows if r.symbol.startswith("920")), rows[0])
    secid = prov._eastmoney_secid(cand.symbol, "BJ")  # noqa: SLF001
    print(f"\n[AB-bj] symbol={cand.symbol} name={cand.name} secid={secid}")
    assert secid == f"0.{cand.symbol}", f"BJ secid 前缀必须为 0，实际 {secid}"

    with httpx.Client(timeout=20.0) as client:
        klines = _kline(
            client, cand.symbol, "BJ", date.today() - timedelta(days=90), date.today()
        )

    assert klines, f"BJ 历史 K 线为空：{cand.symbol}（secid={secid}）"
    last = klines[-1]
    print(f"[AB-bj] last_kline={last}")
    assert float(last[2]) > 0, f"BJ 收盘价非正：{last}"
