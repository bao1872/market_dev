"""pytdx 批量盘后 EOD 快照 provider（G1B-1，未接入任何生产调用方）。

目标：用现有 ``PytdxAdapter.get_security_quotes`` 的批量能力，以约
``ceil(SH_SZ_count / PYTDX_QUOTE_BATCH_SIZE)`` 次网络调用取得全市场 SH/SZ 当天
盘后快照，替代过去 5000+ 次逐股 daily K 请求。

本轮边界（明确不做什么）：
- 不接 scheduler / bars_scheduler_service / eod_daily_refresh_service（零 production caller）。
- 不调 get_daily_bars / get_xdxr_info / factor / qfq / DB / Redis。
- 不发明 volume 单位转换：pytdx quote ``vol`` 单位本轮标记为 ``UNVERIFIED``，
  由 G1B-2 的 A/B 结果确认（仓库 auction_quote_provider 注释提示为「手」）。
- 不把 pytdx ``servertime`` 当作可靠交易所 watermark：``updated_at`` 仅记录本地
  抓取时间；原始 ``servertime`` 字符串保留在 ``raw_servertime_by_symbol`` 供审查。

provider 仅完成「批量抓取 + 标准字段解析 + 严格 identity 校验」，是否可作为
EOD authoritative row 的最终决定留给 G1B-2（含 A/B 实测）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.pytdx_adapter import PytdxSourceError
from app.services.eod_market_snapshot_provider import EodSnapshotRow

if TYPE_CHECKING:
    from app.core.pytdx_adapter import PytdxAdapter
    from app.models.instrument import Instrument

logger = logging.getLogger(__name__)

# 复用现有冻结事实：auction_quote_provider.BATCH_SIZE = 80 是 pytdx
# get_security_quotes 的单次批量上限（经验值，超过可能超时）。本轮不另起常量值。
PYTDX_QUOTE_BATCH_SIZE = 80

# pytdx quote vol 单位本轮未证实；G1B-2 须用 A/B 确认（仓库 auction_quote_provider
# 注释提示为「手」= lots，Eastmoney f5 已按 SHARES_PER_LOT 转股；此处不擅自转换）。
PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED = "UNVERIFIED"


class PytdxEodSnapshotError(RuntimeError):
    """pytdx 批量 EOD 快照整体失败（identity 损坏 / provider 不可用）。

    合法成功但某只 symbol 缺失 → **不**抛本错误，记入 missing_symbols。
    provider 抛 PytdxSourceError → 包装为本错误上抛，不返回空 snapshot 冒充成功。
    """


@dataclass(frozen=True)
class PytdxEodSnapshot:
    """一次 pytdx 批量 EOD 快照的结果（纯 raw 市场事实，未接生产）。

    字段语义（G1B-1 临时约定，待 G1B-2 修正）：
    - ``rows``：成功解析的 EodSnapshotRow；顺序与输入未必一致。
    - ``volume``（在 row 内）：原始 pytdx ``vol`` 值，单位 ``volume_unit`` 未证实，
      不得当作 canonical 股 直接使用。
    - ``updated_at``（在 row 内）：本地抓取时间，不是 provider 交易所 watermark。
    - ``raw_servertime_by_symbol``：pytdx 原始 servertime 字符串，供 G1B-2 审查。
    """

    rows: tuple[EodSnapshotRow, ...]
    requested_count: int
    returned_count: int
    missing_symbols: tuple[str, ...]
    captured_at: datetime
    source: str = "pytdx"
    volume_unit: str = PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED
    raw_servertime_by_symbol: Mapping[str, str] | None = None


def _chunks(items: Sequence[Instrument], size: int) -> Iterator[list[Instrument]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _to_decimal(value: Any) -> Decimal | None:
    """安全转为 Decimal；None / 非数值 / NaN 返回 None。不做任何修正。"""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if d != d:  # NaN 检查
        return None
    return d


def _normalize_quote_row(
    inst: Instrument,
    raw: dict[str, Any],
    captured_at: datetime,
) -> EodSnapshotRow:
    """单个 pytdx quote → EodSnapshotRow。

    严格「不修正」：OHLC 负值 / high<low / 非有限值均原样落库，绝不变造成正常数字。
    ``vol`` 单位未证实，存原始值（不 ×100）。
    """
    return EodSnapshotRow(
        symbol=inst.symbol,
        name=inst.name,
        market=inst.market,
        # 仅本地抓取时间；pytdx 无可靠交易所 watermark（见 get_realtime_quote）。
        updated_at=captured_at,
        open=_to_decimal(raw.get("open")),
        high=_to_decimal(raw.get("high")),
        low=_to_decimal(raw.get("low")),
        close=_to_decimal(raw.get("price")),
        # vol 单位 UNVERIFIED：存原始值，不 ×100，不宣称 canonical 股。
        volume=_to_decimal(raw.get("vol")),
        amount=_to_decimal(raw.get("amount")),
        previous_close=_to_decimal(raw.get("last_close")),
    )


def fetch_pytdx_eod_snapshot(
    adapter: PytdxAdapter,
    instruments: Sequence[Instrument],
    *,
    trade_date: date,
    captured_at: datetime | None = None,
) -> PytdxEodSnapshot:
    """批量抓取 SH/SZ 当天盘后快照（pytdx 主源）。

    BJ 不请求、不报错、不伪造（pytdx 标准接口不支持 BSE）。

    Args:
        adapter: PytdxAdapter 实例（连接 / 生命周期由调用方负责）。
        instruments: 候选标的；仅 ``market in ("SH", "SZ")`` 进入请求。
        trade_date: 业务交易日（本轮仅用于诊断日志，不用于伪造 watermark）。
        captured_at: 本地抓取时间；缺省取 Asia/Shanghai now。

    Returns:
        PytdxEodSnapshot：rows 为成功解析的 EodSnapshotRow。

    Raises:
        PytdxEodSnapshotError: identity 损坏（未请求 symbol / 重复 code）或
            provider PytdxSourceError 包装（绝不返回空 snapshot 冒充成功）。
    """
    if captured_at is None:
        captured_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    supported = [inst for inst in instruments if inst.market in ("SH", "SZ")]
    requested_symbols = {inst.symbol for inst in supported}
    logger.debug("pytdx eod snapshot trade_date=%s supported=%d", trade_date, len(supported))

    quote_by_symbol: dict[str, dict[str, Any]] = {}
    raw_servertime: dict[str, str] = {}

    try:
        for batch in _chunks(supported, PYTDX_QUOTE_BATCH_SIZE):
            symbols = [inst.symbol for inst in batch]
            raw_rows = adapter.get_security_quotes(symbols)
            for raw in raw_rows:
                code = str(raw.get("code") or "")
                if not code:
                    raise PytdxEodSnapshotError(f"quote row missing code: {raw!r}")
                if code not in requested_symbols:
                    raise PytdxEodSnapshotError(
                        f"quote returned unrequested symbol not in batch: {code}"
                    )
                if code in quote_by_symbol:
                    raise PytdxEodSnapshotError(f"duplicate symbol returned by pytdx: {code}")
                quote_by_symbol[code] = raw
                servertime = raw.get("servertime")
                if servertime is not None:
                    raw_servertime[code] = str(servertime)
    except PytdxSourceError as exc:
        raise PytdxEodSnapshotError(
            f"pytdx source unavailable during batch eod snapshot: {exc}"
        ) from exc

    rows: list[EodSnapshotRow] = []
    for inst in supported:
        raw = quote_by_symbol.get(inst.symbol)
        if raw is None:
            # 合法成功但缺失：不伪造 row，记入 missing_symbols
            continue
        rows.append(_normalize_quote_row(inst, raw, captured_at))

    missing_symbols = tuple(
        inst.symbol for inst in supported if inst.symbol not in quote_by_symbol
    )

    return PytdxEodSnapshot(
        rows=tuple(rows),
        requested_count=len(supported),
        returned_count=len(rows),
        missing_symbols=missing_symbols,
        captured_at=captured_at,
        source="pytdx",
        volume_unit=PYTDX_QUOTE_VOLUME_UNIT_UNVERIFIED,
        raw_servertime_by_symbol=raw_servertime or None,
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
    quote_rows: Sequence[EodSnapshotRow],
    reference_rows: Sequence[Any],
) -> dict[str, Any]:
    """纯函数 A/B 诊断：比较 pytdx quote 与 daily K 参考（手动运行入口，不联网/不连 DB）。

    ``reference_rows`` 中每个元素需能被 ``_item_get`` 读取
    ``symbol/open/high/low/close/volume/amount``（EodSnapshotRow 或 Mapping 均可）。

    输出 ``volume_ratio`` / ``amount_ratio`` 用于判断 pytdx ``vol`` 是「手」还是「股」：
    若 ``quote.volume / reference.volume ≈ 0.01``（即 100 倍），则 pytdx vol 为手、
    需 ×100 转 canonical 股；若 ≈ 1.0 则已为股。G1B-2 据此外部 A/B 后决定单位。

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
        if q.volume is not None and rv not in (None, 0):
            entry["volume_ratio"] = float(q.volume) / float(rv)
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
