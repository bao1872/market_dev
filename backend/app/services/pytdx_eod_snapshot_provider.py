"""pytdx 批量盘后 EOD 行情快照 provider（G1B-1.1，未接入任何生产调用方）。

本轮修正（G1B-1.1）：彻底移除对 canonical ``EodSnapshotRow`` 的复用，改用独立的
**raw pytdx quote DTO** ``PytdxQuoteSnapshotRow``。原因：``EodSnapshotRow.volume`` 合同是
canonical 股、``updated_at`` 决定 authoritative trade_date；但 pytdx 当前 ``vol`` 单位未 A/B
证明、``servertime`` 无日期、``captured_at`` 只是本机抓取时间。把未验证事实塞进 canonical
行会制造类型合同欺骗，不能靠注释消除。G1B-2 A/B 验证通过后再写唯一的 raw → canonical 转换。

本轮边界（明确不做什么）：
- 不接 scheduler / bars_scheduler_service / eod_daily_refresh_service（零 production caller）。
- 不调 get_daily_bars / get_xdxr_info / factor / qfq / DB / Redis。
- 不发明 volume 单位转换：``raw_volume`` 是原始 pytdx ``vol``，单位 ``volume_unit="UNVERIFIED"``，
  由 G1B-2 的 A/B 结果确认（仓库 auction_quote_provider 注释提示为「手」）。
- 不把 pytdx ``servertime`` 当成可靠交易所 watermark / trade_date：只以 ``source_time`` 字符串保存。
- identity 以「当前 batch + (market, code)」为边界；输入未知 market / 重复 identity fail-closed。
- 复用了 auction_quote_provider 已冻结的批量上限与限流 precedent（BATCH_SIZE=80 / 0.3s）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.pytdx_adapter import MARKET_NAME_TO_CODE, PytdxSourceError

if TYPE_CHECKING:
    from app.core.pytdx_adapter import PytdxAdapter
    from app.models.instrument import Instrument

logger = logging.getLogger(__name__)

# 复用现有冻结事实：auction_quote_provider.BATCH_SIZE = 80 是 pytdx
# get_security_quotes 的单次批量上限（经验值，超过可能超时）。本轮不另起常量值。
PYTDX_QUOTE_BATCH_SIZE = 80

# 复用现有冻结 precedent：auction_quote_provider.BATCH_INTERVAL_SECONDS = 0.3。
# 每批之间 sleep，防止 pytdx 连续批量请求把连接打断。
PYTDX_QUOTE_BATCH_INTERVAL_SECONDS = 0.3

# pytdx quote vol 单位本轮未证实；G1B-2 须用 A/B 确认（仓库 auction_quote_provider
# 注释提示为「手」= lots，Eastmoney f5 已按 SHARES_PER_LOT 转股；此处不擅自转换）。
PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED = "UNVERIFIED"


class PytdxEodSnapshotError(RuntimeError):
    """pytdx 批量 EOD 行情快照整体失败（identity 损坏 / provider 不可用 / 输入非法）。

    合法成功但某只 symbol 缺失 → **不**抛本错误，记入 missing_symbols。
    provider 抛 PytdxSourceError → 包装为本错误上抛，不返回空 snapshot 冒充成功。
    """


@dataclass(frozen=True)
class PytdxQuoteSnapshotRow:
    """单只 pytdx 原始盘后行情快照（raw，未经证明）。

    字段语义（G1B-1.1 临时 raw 合同，待 G1B-2 转换）：
    - ``raw_volume``：**明确不是** canonical volume；是原始 pytdx ``vol``，单位未证实。
    - ``source_time``：pytdx 原始 ``servertime`` 字符串（"H:M:S"，无日期），仅诊断，
      不构成 trade_date / watermark。
    - ``captured_at``：本地抓取时刻，仅记录用。
    - 不提供 ``trade_date`` / ``updated_at`` / ``volume``（canonical）等属性。
    - ``raw_payload``：保留原始证据，使转换成 None 不是 silent loss。
    """

    symbol: str
    name: str
    market: str

    captured_at: datetime
    source_time: str | None

    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None

    raw_volume: Decimal | None
    amount: Decimal | None
    previous_close: Decimal | None

    raw_payload: Mapping[str, Any]


@dataclass(frozen=True)
class PytdxEodSnapshot:
    """一次 pytdx 批量 EOD 行情快照的结果（纯 raw 市场事实，未接生产）。

    ``requested_trade_date`` 只是调用上下文（业务交易日），不是 provider 已证明的
    authoritative trade_date；不得通过 ``captured_at.date()`` 自动制造 trade_date。
    """

    rows: tuple[PytdxQuoteSnapshotRow, ...]
    requested_trade_date: date
    requested_count: int
    returned_count: int
    missing_symbols: tuple[str, ...]
    captured_at: datetime
    source: str = "pytdx"
    volume_unit: str = PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED


def _chunks(items: Sequence[Instrument], size: int) -> Iterator[list[Instrument]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _to_decimal(value: Any) -> Decimal | None:
    """安全转为 Decimal；None / 非数值 / 非有限（NaN、Infinity）返回 None。

    返回 None 仅代表“本字段无法解析为有限 Decimal”，原始值仍保存在
    ``PytdxQuoteSnapshotRow.raw_payload``，不是 silent correction。
    """
    if value is None:
        return None
    try:
        value_decimal = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if not value_decimal.is_finite():
        return None
    return value_decimal


def _normalize_quote_row(
    inst: Instrument,
    raw: dict[str, Any],
    captured_at: datetime,
) -> PytdxQuoteSnapshotRow:
    """单个 pytdx raw quote → raw DTO。

    严格「不修正」：负值 / high<low / 非有限值均原样落库（或按 _to_decimal 规则置 None 并保留
    raw_payload），绝不变造成正常数字。``raw_volume`` 单位未证实，存原始值（不 ×100）。
    """
    source_time = str(raw["servertime"]) if raw.get("servertime") is not None else None
    return PytdxQuoteSnapshotRow(
        symbol=inst.symbol,
        name=inst.name,
        market=inst.market,
        captured_at=captured_at,
        source_time=source_time,
        open=_to_decimal(raw.get("open")),
        high=_to_decimal(raw.get("high")),
        low=_to_decimal(raw.get("low")),
        close=_to_decimal(raw.get("price")),
        raw_volume=_to_decimal(raw.get("vol")),
        amount=_to_decimal(raw.get("amount")),
        previous_close=_to_decimal(raw.get("last_close")),
        raw_payload=dict(raw),
    )


def fetch_pytdx_eod_snapshot(
    adapter: PytdxAdapter,
    instruments: Sequence[Instrument],
    *,
    trade_date: date,
    captured_at: datetime | None = None,
    batch_interval_seconds: float = PYTDX_QUOTE_BATCH_INTERVAL_SECONDS,
) -> PytdxEodSnapshot:
    """批量抓取 SH/SZ 当天盘后行情（pytdx 主源）。

    BJ 合法跳过（pytdx 标准接口不支持 BSE）；其他未知 market fail-closed 报错。
    重复输入 (market, symbol) 在网络请求前报错。

    Args:
        adapter: PytdxAdapter 实例（连接 / 生命周期由调用方负责）。
        instruments: 候选标的。
        trade_date: 业务交易日（仅作为 ``requested_trade_date`` 上下文，不用于伪造 watermark）。
        captured_at: 本地抓取时间；缺省取 Asia/Shanghai now。
        batch_interval_seconds: 每批之间 sleep 秒数（复用 auction provider 0.3s precedent）。

    Returns:
        PytdxEodSnapshot：rows 为成功解析的 PytdxQuoteSnapshotRow。

    Raises:
        PytdxEodSnapshotError: 输入非法（未知 market / 重复 identity）/ identity 损坏
            （返回 identity 越出当前 batch / 重复 code / market 不匹配）/ provider 不可用。
    """
    if captured_at is None:
        captured_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 输入过滤 + fail-closed：SH/SZ 请求，BJ 跳过，其他 market 报错。
    supported: list[Instrument] = []
    for inst in instruments:
        market = inst.market
        if market in ("SH", "SZ"):
            supported.append(inst)
        elif market == "BJ":
            continue
        else:
            raise PytdxEodSnapshotError(
                f"unsupported market for pytdx quote: {market!r} "
                f"(symbol={inst.symbol}; pytdx 仅支持 SH/SZ，BJ 由外部 fallback)"
            )

    # 重复输入 (market, symbol) 在网络请求之前报错。
    seen_input_identity: set[tuple[str, str]] = set()
    for inst in supported:
        key = (inst.market, inst.symbol)
        if key in seen_input_identity:
            raise PytdxEodSnapshotError(f"duplicate input identity: {key}")
        seen_input_identity.add(key)

    batches = list(_chunks(supported, PYTDX_QUOTE_BATCH_SIZE))
    matched: list[tuple[Instrument, dict[str, Any]]] = []

    try:
        for batch_index, batch in enumerate(batches):
            batch_identity: dict[tuple[int, str], Instrument] = {}
            for inst in batch:
                expected_market = MARKET_NAME_TO_CODE[inst.market]
                key = (expected_market, inst.symbol)
                if key in batch_identity:
                    raise PytdxEodSnapshotError(f"duplicate requested identity: {key}")
                batch_identity[key] = inst

            symbols = [inst.symbol for inst in batch]
            raw_rows = adapter.get_security_quotes(symbols)

            returned_identity: set[tuple[int, str]] = set()
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise PytdxEodSnapshotError(f"quote row must be mapping: {raw!r}")
                market_raw = raw.get("market")
                code_raw = raw.get("code")
                if type(market_raw) is not int:
                    raise PytdxEodSnapshotError(f"quote market invalid: {market_raw!r}")
                if not isinstance(code_raw, str) or not code_raw:
                    raise PytdxEodSnapshotError(f"quote code invalid: {code_raw!r}")

                key = (market_raw, code_raw)
                inst = batch_identity.get(key)
                if inst is None:
                    raise PytdxEodSnapshotError(
                        f"quote returned identity outside current batch: {key}"
                    )
                if key in returned_identity:
                    raise PytdxEodSnapshotError(f"duplicate quote identity: {key}")
                returned_identity.add(key)
                matched.append((inst, dict(raw)))

            if batch_index < len(batches) - 1 and batch_interval_seconds > 0:
                time.sleep(batch_interval_seconds)
    except PytdxSourceError as exc:
        raise PytdxEodSnapshotError(
            f"pytdx source unavailable during batch eod snapshot: {exc}"
        ) from exc

    matched_keys = {
        (MARKET_NAME_TO_CODE[inst.market], inst.symbol) for inst, _ in matched
    }
    missing_symbols = tuple(
        inst.symbol
        for inst in supported
        if (MARKET_NAME_TO_CODE[inst.market], inst.symbol) not in matched_keys
    )
    rows = tuple(
        _normalize_quote_row(inst, raw, captured_at) for inst, raw in matched
    )

    return PytdxEodSnapshot(
        rows=rows,
        requested_trade_date=trade_date,
        requested_count=len(supported),
        returned_count=len(rows),
        missing_symbols=missing_symbols,
        captured_at=captured_at,
        source="pytdx",
        volume_unit=PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED,
    )


def _item_get(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def compare_quote_to_daily_reference(
    quote_rows: Sequence[PytdxQuoteSnapshotRow],
    reference_rows: Sequence[Any],
) -> dict[str, Any]:
    """纯函数 A/B 诊断：比较 pytdx raw quote 与 daily K 参考（手动运行入口，不联网/不连 DB）。

    ``reference_rows`` 中每个元素需能被 ``_item_get`` 读取
    ``symbol/open/high/low/close/volume/amount``（canonical EodSnapshotRow 或 Mapping 均可）。

    输出 ``volume_ratio`` / ``amount_ratio`` 用于判断 pytdx ``vol`` 是「手」还是「股」：
    ``q.raw_volume / reference.volume ≈ 0.01`` 即 quote 为手、需 ×100 转 canonical 股；
    ``≈ 1.0`` 则已为股。G1B-2 据此外部 A/B 后决定单位。

    external_data 标记：本函数仅供手动/外部 A/B 运行，不应进入普通 pytest 生产断言。
    """
    ref_by_symbol = {_item_get(r, "symbol"): r for r in reference_rows}
    per_symbol: list[dict[str, Any]] = []
    for q in quote_rows:
        ref = ref_by_symbol.get(q.symbol)
        if ref is None:
            continue
        entry: dict[str, Any] = {"symbol": q.symbol}
        for fld in ("open", "high", "low", "close", "amount"):
            qv = getattr(q, fld)
            rv = _item_get(ref, fld)
            if qv is not None and rv not in (None, 0):
                entry[f"{fld}_ratio"] = float(qv) / float(rv)
        rv = _item_get(ref, "volume")
        if q.raw_volume is not None and rv not in (None, 0):
            entry["volume_ratio"] = float(q.raw_volume) / float(rv)
        per_symbol.append(entry)

    vol_ratios = [e["volume_ratio"] for e in per_symbol if "volume_ratio" in e]
    amt_ratios = [e["amount_ratio"] for e in per_symbol if "amount_ratio" in e]
    return {
        "compared_count": len(per_symbol),
        "volume_ratio_median": _median(vol_ratios),
        "volume_ratio_mean": _mean(vol_ratios),
        "amount_ratio_median": _median(amt_ratios),
        "amount_ratio_mean": _mean(amt_ratios),
        "per_symbol": per_symbol,
    }
