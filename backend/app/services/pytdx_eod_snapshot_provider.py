"""pytdx 批量盘后 EOD 行情快照 provider（G1B-1.1，未接入任何生产调用方）。

G1B-2A 状态（2026-09-12 完成）：raw quote DTO + **verified snapshot** + **唯一 canonical
converter** 三段式已经落地：

1. :func:`fetch_pytdx_eod_snapshot` —— 只调 ``get_security_quotes``，产出 raw
   :class:`PytdxEodSnapshot`（``raw_volume`` 保留原始 quote ``vol``）。
2. :func:`verify_pytdx_eod_snapshot` —— 用 **1 只 SH + 1 只 SZ** 的 exact-date daily
   sentinel 证明 ``verified_trade_date``（绝不来自 ``captured_at.date()`` / ``source_time``）。
3. :func:`to_canonical_eod_rows` —— 唯一 raw→canonical 转换：``volume = raw_volume × 100``
   （手→股），``updated_at = verified_trade_date + source_time``（Asia/Shanghai）。

单位事实（direct 对照证明，见 docs/changes/2026/CHANGE-20260912-001）：
- ``get_security_quotes().vol`` = **手（lots）** → canonical ×100；
- ``get_daily_bars().volume`` = **股（shares）** → 原样，**禁止 ×100**。

边界（明确不做什么）：
- 不接 scheduler / bars_scheduler_service / eod_daily_refresh_service（G1B-2B 才接）。
- raw fetch **不调** ``get_daily_bars``；只有 :func:`verify_pytdx_eod_snapshot` 调 2 次。
- ``raw_volume`` 永不改名为 canonical ``volume``；换算只在 converter 发生。
- ``servertime`` 只证明「收盘时刻」（>= 15:00），**不证明日期**。
- identity 以「当前 batch + (market, code)」为边界；输入未知 market / 重复 identity fail-closed。
- 复用了 auction_quote_provider 已冻结的批量上限与限流 precedent（BATCH_SIZE=80 / 0.3s）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.core.pytdx_adapter import MARKET_NAME_TO_CODE, PytdxSourceError

if TYPE_CHECKING:
    from app.core.pytdx_adapter import PytdxAdapter
    from app.models.instrument import Instrument
    from app.services.eod_market_snapshot_provider import EodSnapshotRow

logger = logging.getLogger(__name__)

# 复用现有冻结事实：auction_quote_provider.BATCH_SIZE = 80 是 pytdx
# get_security_quotes 的单次批量上限（经验值，超过可能超时）。本轮不另起常量值。
PYTDX_QUOTE_BATCH_SIZE = 80

# 复用现有冻结 precedent：auction_quote_provider.BATCH_INTERVAL_SECONDS = 0.3。
# 每批之间 sleep，防止 pytdx 连续批量请求把连接打断。
PYTDX_QUOTE_BATCH_INTERVAL_SECONDS = 0.3

# G1B-2A 结论（2026-09-12，direct 对照已证明）：quote ``vol`` = **手（lots）**，
# canonical ``volume`` = 股 → ×100。
# 证据：**同一 server、同一 symbol**，``quote.vol / daily.volume`` median = 0.00999998937
# （4 台独立 server × 40 只，min 0.0099989839 / max 0.0100000000），``amount`` ratio = 1.0；
# 而 ``get_daily_bars().volume`` 与 canonical DB 的 ratio median = 1.0（即 daily 已是股）。
# 详见 docs/changes/2026/CHANGE-20260912-001。
#
# 注意：``PytdxQuoteSnapshotRow.raw_volume`` **仍保存原始 quote vol（手）**，fetch 阶段
# 不做任何换算；唯一换算发生在 :func:`to_canonical_eod_rows`。
PYTDX_QUOTE_VOLUME_UNIT_LOTS = "LOTS"
PYTDX_QUOTE_LOT_TO_SHARES = Decimal("100")

# 收盘时间严格解析的界：``servertime`` 只证明「收盘时刻」，**不证明日期**。
_QUOTE_CLOSE_CUTOFF = dt_time(15, 0, 0)

# sentinel 比较容差（G1B-2A）：
#   价格：绝对差 <= 0.01 元；
#   数量：converted(quote × 100) 与 daily 的相对误差 <= 0.2%（真实最坏 ~0.01%，余量充足，
#         同时远小于「单位差 100 倍」这种量级错误）；
#   成交额：相对误差 <= 2%。
_PRICE_TOL = Decimal("0.01")
_VOLUME_REL_TOL = Decimal("0.002")
_AMOUNT_REL_TOL = Decimal("0.02")


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
    volume_unit: str = PYTDX_QUOTE_VOLUME_UNIT_LOTS


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
        volume_unit=PYTDX_QUOTE_VOLUME_UNIT_LOTS,
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


# ── G1B-2A：verified snapshot + 唯一 raw → canonical converter ────────
def _parse_close_time(value: object) -> dt_time:
    """严格解析 pytdx ``servertime``：必须是合法时间且 >= 15:00:00，否则 fail-closed。

    只接受真实格式（``15:30:25`` / ``15:30:25.650``）。``servertime`` 只证明
    「收盘时刻」，**不证明日期**（无日期分量）——日期只能来自 daily sentinel。
    """
    if not isinstance(value, str) or not value.strip():
        raise PytdxEodSnapshotError(f"quote source_time missing: {value!r}")
    raw = value.strip()
    parsed: dt_time | None = None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt).time()
            break
        except ValueError:
            continue
    if parsed is None:
        raise PytdxEodSnapshotError(f"quote source_time malformed: {value!r}")
    if parsed < _QUOTE_CLOSE_CUTOFF:
        raise PytdxEodSnapshotError(f"quote source_time before 15:00: {value!r}")
    return parsed


def _relative_error(actual: Decimal, expected: Decimal) -> Decimal:
    base = abs(expected)
    if base == 0:
        return Decimal("0") if actual == 0 else Decimal("Infinity")
    return abs(actual - expected) / base


@dataclass(frozen=True)
class VerifiedPytdxEodSnapshot:
    """经 exact-date daily sentinel 证明过交易日的 raw quote 快照。

    ``verified_trade_date`` **只能**来自 daily sentinel 的 exact-date proof；
    绝不能来自 ``captured_at.date()``、``source_time`` 或任何 wall-clock 推断。
    """

    raw_snapshot: PytdxEodSnapshot
    verified_trade_date: date
    sentinel_symbols: tuple[str, ...]


def _is_sentinel_candidate(row: PytdxQuoteSnapshotRow) -> bool:
    """sentinel 候选有效性：SH/SZ + OHLC/vol/amount 有限且 > 0 + source_time >= 15:00。"""
    if row.market not in ("SH", "SZ"):
        return False
    for value in (row.open, row.high, row.low, row.close, row.raw_volume, row.amount):
        if value is None or not value.is_finite() or value <= 0:
            return False
    try:
        _parse_close_time(row.source_time)
    except PytdxEodSnapshotError:
        return False
    return True


def _verify_sentinel_row(
    adapter: PytdxAdapter,
    row: PytdxQuoteSnapshotRow,
    trade_date: date,
) -> None:
    """单个 sentinel：exact-date daily 对照（价格 / 数量 / 成交额）。"""
    daily = adapter.get_daily_bars(row.symbol, trade_date, trade_date)
    if daily is None or daily.empty or len(daily) != 1:
        raise PytdxEodSnapshotError(
            f"sentinel daily exact-date 失败 symbol={row.symbol} "
            f"date={trade_date} rows={0 if daily is None else len(daily)}"
        )
    bar = daily.iloc[0]
    bar_date = pd.Timestamp(bar["datetime"]).date()
    if bar_date != trade_date:
        raise PytdxEodSnapshotError(
            f"sentinel daily date mismatch symbol={row.symbol} "
            f"got={bar_date} expect={trade_date}"
        )

    for label, quote_value, column in (
        ("open", row.open, "open"),
        ("high", row.high, "high"),
        ("low", row.low, "low"),
        ("close", row.close, "close"),
    ):
        daily_value = _to_decimal(bar[column])
        if daily_value is None or abs(quote_value - daily_value) > _PRICE_TOL:
            raise PytdxEodSnapshotError(
                f"sentinel {label} mismatch symbol={row.symbol} "
                f"quote={quote_value} daily={daily_value}"
            )

    daily_volume = _to_decimal(bar["volume"])
    if daily_volume is None or daily_volume <= 0:
        raise PytdxEodSnapshotError(
            f"sentinel daily volume unusable symbol={row.symbol} value={daily_volume}"
        )
    converted = row.raw_volume * PYTDX_QUOTE_LOT_TO_SHARES
    if _relative_error(converted, daily_volume) > _VOLUME_REL_TOL:
        raise PytdxEodSnapshotError(
            f"sentinel volume mismatch symbol={row.symbol} "
            f"converted={converted} daily={daily_volume} "
            f"rel={_relative_error(converted, daily_volume)}"
        )

    daily_amount = _to_decimal(bar["amount"])
    if daily_amount is None or daily_amount <= 0:
        raise PytdxEodSnapshotError(
            f"sentinel daily amount unusable symbol={row.symbol} value={daily_amount}"
        )
    if _relative_error(row.amount, daily_amount) > _AMOUNT_REL_TOL:
        raise PytdxEodSnapshotError(
            f"sentinel amount mismatch symbol={row.symbol} "
            f"quote={row.amount} daily={daily_amount} "
            f"rel={_relative_error(row.amount, daily_amount)}"
        )


def verify_pytdx_eod_snapshot(
    adapter: PytdxAdapter,
    snapshot: PytdxEodSnapshot,
) -> VerifiedPytdxEodSnapshot:
    """用 1 只 SH + 1 只 SZ 的 exact-date daily sentinel 证明交易日。

    sentinel 选取：按 ``snapshot.rows`` 的稳定顺序，取第一只有效候选（不 hard-code 股票）。
    SH / SZ 任一缺失，或任一 sentinel 对照失败 → :class:`PytdxEodSnapshotError`
    （不产生 ``VerifiedPytdxEodSnapshot``，不静默降级）。

    Raises:
        PytdxEodSnapshotError: sentinel 不足 / daily exact-date 失败 / 价格或数量或成交额不符。
    """
    sentinels: list[PytdxQuoteSnapshotRow] = []
    for market in ("SH", "SZ"):
        for row in snapshot.rows:
            if row.market == market and _is_sentinel_candidate(row):
                sentinels.append(row)
                break

    if len(sentinels) != 2:
        raise PytdxEodSnapshotError(
            "sentinel 不足：需要 1 只 SH + 1 只 SZ 的有效 quote row，"
            f"got={[r.symbol for r in sentinels]}"
        )

    for row in sentinels:
        _verify_sentinel_row(adapter, row, snapshot.requested_trade_date)

    return VerifiedPytdxEodSnapshot(
        raw_snapshot=snapshot,
        verified_trade_date=snapshot.requested_trade_date,
        sentinel_symbols=tuple(row.symbol for row in sentinels),
    )


def to_canonical_eod_rows(
    verified: VerifiedPytdxEodSnapshot,
) -> tuple[EodSnapshotRow, ...]:
    """唯一 raw → canonical 转换（必须传入 verified snapshot）。

    - ``volume = raw_volume × PYTDX_QUOTE_LOT_TO_SHARES``（手 → 股）；
    - ``updated_at = combine(verified_trade_date, source_time, Asia/Shanghai)``
      —— **不是** ``captured_at`` 的日期；
    - 任一 returned row 的 ``source_time`` 缺失 / 畸形 / < 15:00 → 整个 snapshot
      fail-closed（不静默丢行）。

    Raises:
        TypeError: 传入的不是 :class:`VerifiedPytdxEodSnapshot`（防止绕过 verifier）。
        PytdxEodSnapshotError: 任一 row 的 ``source_time`` 不可用。
    """
    if not isinstance(verified, VerifiedPytdxEodSnapshot):
        raise TypeError(
            "to_canonical_eod_rows 只接受 VerifiedPytdxEodSnapshot：必须先通过 "
            "verify_pytdx_eod_snapshot 的 exact-date daily sentinel 证明"
        )
    from app.services.eod_market_snapshot_provider import EodSnapshotRow

    rows: list[EodSnapshotRow] = []
    for row in verified.raw_snapshot.rows:
        parsed_time = _parse_close_time(row.source_time)
        rows.append(
            EodSnapshotRow(
                symbol=row.symbol,
                name=row.name,
                market=row.market,
                updated_at=datetime.combine(
                    verified.verified_trade_date,
                    parsed_time,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=(
                    row.raw_volume * PYTDX_QUOTE_LOT_TO_SHARES
                    if row.raw_volume is not None
                    else None
                ),
                amount=row.amount,
                previous_close=row.previous_close,
            )
        )
    return tuple(rows)
