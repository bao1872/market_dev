"""东方财富全市场收盘快照 Provider（非常薄、可测试、不复权）。

职责：
- 通过东方财富 push2 ``clist/get`` 接口分页拉取全市场 A 股收盘快照（盘后）。
- 归一化为项目标准 :class:`EodSnapshotRow`（raw OHLCV，不复权；volume=手，amount=元）。
- 不做任何复权、不做任何指标计算。

fail-closed 契约（详见各函数 docstring）：
- 分页中间页网络异常必须重试；最终失败则整体 fail，禁止 ``except: break`` 半截返回。
- ``total`` 与实拉行数不一致必须 fail-closed。
- ``trade_date`` 必须来自 ``f124`` 时间戳（Asia/Shanghai），老时间戳/停牌/空价不伪造 K 线。

禁止：
- 不依赖 ``qstock``（项目锁定 1.3.1）。
- 不写前复权价格；raw 与 qfq 的区分只在 adj_factor 层（由现有 factor owner 处理）。

单位约定（**已由外部实测校正**，见 tests/test_eod_external_ab.py）：
- ``f5`` = 成交量，单位「手」；``f6`` = 成交额，单位「元」。
- 价格（``f2/f15/f16/f17/f18``）在 ``fltt=2`` 下**已是元**，不需要再缩放。
  实测依据：``f6 ≈ f5 × 100 × f2`` 在 100 只样本上最大相对误差 6.5%
  （差异来自盘中采样时刻不一致）；若价格是「分」，该等式会相差 100 倍。
  历史上曾误设为按「分」÷100，已由实测推翻。
- ``clist`` 接口的 ``pz`` 参数**实际上限为 100**：传入更大值会被静默截断为 100 行，
  因此分页必须依赖 ``total`` 而非「单页取满」来判定结束。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.services.instrument_maintenance_service import is_stock_symbol

logger = logging.getLogger(__name__)

# 全市场快照主机候选（按顺序尝试）。
# 实测：push2 在部分网络被拒（RemoteProtocolError），push2delay 稳定可达；
# 二者为同一 API，盘后（本链路唯一使用场景）返回的收盘数据为终值。
EASTMONEY_CLIST_HOSTS: tuple[str, ...] = (
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
)
# 历史 K 线主机候选。
EASTMONEY_HIS_HOSTS: tuple[str, ...] = (
    "push2his.eastmoney.com",
    "push2delay.eastmoney.com",
)

_CLIST_PATH = "/api/qt/clist/get"
_KLINE_PATH = "/api/qt/stock/kline/get"

EASTMONEY_CLIST_URL = f"https://{EASTMONEY_CLIST_HOSTS[0]}{_CLIST_PATH}"
EASTMONEY_KLINE_URL = f"https://{EASTMONEY_HIS_HOSTS[0]}{_KLINE_PATH}"

# 全市场 A 股过滤（沪深京）。与用户给定筛选一致。
A_SHARE_FILTER = (
    "m:0+t:6,"
    "m:0+t:80,"
    "m:1+t:2,"
    "m:1+t:23,"
    "m:0+t:81+s:2048"
)

FIELDS = ",".join([
    "f12",   # code
    "f14",   # name
    "f13",   # market
    "f2",    # latest / 收盘
    "f15",   # high
    "f16",   # low
    "f17",   # open
    "f5",    # volume 手
    "f6",    # amount 元
    "f18",   # previous close
    "f124",  # update timestamp（秒）
])

# clist 的 pz 上限实测为 100；超过该值会被静默截断，故显式使用 100。
DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_RETRIES = 3
_PAGE_RETRY_BASE_DELAY = 1.0

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 当日快照最早可视为「收盘终值」的时刻：收盘 15:00 后留 5 分钟结算余量。
# 该守卫的唯一目的是防止盘前/盘中把实时价写成当日最终日线。
_EOD_READY_TIME = time(15, 5)

# Eastmoney secid 市场前缀（实测：沪深京统一行情下北交所亦为 0，不是 2）。
# 见 tests/test_eod_snapshot_provider.py::test_eastmoney_secid_bj_is_zero_prefix
_EM_SECID_MARKET = {"SH": "1", "SZ": "0", "BJ": "0"}


class SnapshotProviderError(RuntimeError):
    """全市场 snapshot 整体失败（网络重试耗尽 / 数据不完整 / payload 非法）。

    上层捕获后退回 legacy 逐股 daily 路径，禁止静默吞掉。
    """


@dataclass(frozen=True)
class EodSnapshotRow:
    """单只 A 股收盘快照（raw / 不复权）。

    volume 单位「手」，amount 单位「元」。trade_date 为 Asia/Shanghai 日期，
    取自 f124；无法判定时为 None（不伪造 K 线）。
    """

    symbol: str
    name: str
    market: str          # SH / SZ / BJ
    trade_date: date | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None   # 手
    amount: Decimal | None   # 元
    previous_close: Decimal | None


def classify_a_share_market(symbol: str, eastmoney_market: Any) -> str:
    """前缀 + f13 双重校验后返回市场（SH / SZ / BJ）。fail-closed。

    单一事实源：前缀决定期望市场与期望 f13，f13 必须完全一致，最后再过一遍项目
    现有 ``is_stock_symbol``。任一层不通过即抛 ValueError，由调用方丢弃该行。

    为什么必须校验 f13（而不是只看前缀）：
    - ``000001`` 在沪市是上证指数、在深市是平安银行，同码不同物。只按前缀会把
      沪市指数当成深市股票写坏行情。
    - 北交所在东财统一行情下的 f13 为 0（不是 2），secid 前缀也是 0；若按 2 拼
      secid，北交所历史 K 线永远取不到。

    Args:
        symbol: 6 位 A 股代码。
        eastmoney_market: Eastmoney f13（1=沪，0=深/北）。

    Raises:
        ValueError: 前缀非 A 股、f13 与期望不符、或 is_stock_symbol 拒绝。
    """
    if symbol.startswith("6"):
        expected: tuple[str, int] = ("SH", 1)
    elif symbol.startswith(("92", "43", "83", "87", "88")):
        expected = ("BJ", 0)
    elif symbol.startswith(("00", "02", "30")):
        expected = ("SZ", 0)
    else:
        raise ValueError(f"not A-share stock: {symbol}")

    market, expected_f13 = expected

    try:
        actual_f13 = int(eastmoney_market)
    except (TypeError, ValueError):
        raise ValueError(
            f"market identity unknown: symbol={symbol}, f13={eastmoney_market!r}"
        ) from None

    if actual_f13 != expected_f13:
        raise ValueError(
            f"market identity mismatch: symbol={symbol}, "
            f"expected_f13={expected_f13}, actual_f13={actual_f13}"
        )

    if not is_stock_symbol(symbol, market):
        raise ValueError(
            f"not A-share stock by project rule: symbol={symbol}, market={market}"
        )
    return market


def can_use_same_day_eod_snapshot(
    trade_date: date,
    now: datetime | None = None,
) -> bool:
    """判断 ``trade_date`` 是否可用「今天的实时快照」落库（fail-closed）。

    返回 True 仅当：``trade_date`` 就是上海时区的当天，且当前时间 >= 15:05。

    为什么需要该守卫：实时快照的 f124 在盘中同样等于今天，仅校验
    ``row.trade_date == trade_date`` 会把 13:00 的盘中价当成当日最终日线写库。
    盘后重跑历史交易日（trade_date 为过去）同样必须返回 False —— 不能用今天的
    快照去补昨天的日线，那种情况必须走 historical repair 路径。

    Args:
        trade_date: 目标交易日。
        now: 仅测试注入；默认取当前上海时间。
    """
    current = now or datetime.now(_SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SHANGHAI_TZ)
    else:
        current = current.astimezone(_SHANGHAI_TZ)

    return trade_date == current.date() and current.time() >= _EOD_READY_TIME


def snapshot_trade_date(ts: Any) -> date | None:
    """从 f124 时间戳（秒）转为 Asia/Shanghai 日期。fail-closed。

    返回 None 的情形：空 / 非整数 / <=0 / 转换异常。
    """
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return None
    if ts_int <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts_int, tz=_SHANGHAI_TZ).date()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    """把 Eastmoney 字段安全转 Decimal；空/非法返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value in ("", "-", "--", "None", "nan"):
            return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_price(value: Any) -> Decimal | None:
    """价格字段（f2/f15/f16/f17/f18）。

    ``fltt=2`` 下东方财富**已直接返回元**（实测：``f6 ≈ f5 × 100 × f2``），
    因此这里只做安全解析，**不做任何缩放**。误加 ÷100 会把价格缩小 100 倍。
    """
    return _parse_decimal(value)


def parse_eod_snapshot_row(raw: dict[str, Any]) -> EodSnapshotRow | None:
    """单条 Eastmoney diff -> EodSnapshotRow；非法行返回 None。

    过滤规则（唯一事实源在 :func:`classify_a_share_market`）：前缀分类 + f13 一致性
    + 项目 ``is_stock_symbol``，任一失败即丢弃（含静态 BJ_STOCKS 之外的新北交所
    920xxx 也会被正确接纳）。
    """
    try:
        symbol = str(raw.get("f12", "")).strip()
        name = str(raw.get("f14", "")).strip()
        em_market = raw.get("f13")
    except Exception:
        return None
    if not symbol:
        return None
    try:
        market = classify_a_share_market(symbol, em_market)
    except ValueError:
        return None

    return EodSnapshotRow(
        symbol=symbol,
        name=name,
        market=market,
        trade_date=snapshot_trade_date(raw.get("f124")),
        open=_parse_price(raw.get("f17")),
        high=_parse_price(raw.get("f15")),
        low=_parse_price(raw.get("f16")),
        close=_parse_price(raw.get("f2")),
        volume=_parse_decimal(raw.get("f5")),
        amount=_parse_decimal(raw.get("f6")),
        previous_close=_parse_price(raw.get("f18")),
    )


def normalize_snapshot_rows(raw_rows: Sequence[dict[str, Any]]) -> list[EodSnapshotRow]:
    """批量归一化 + 过滤为 EodSnapshotRow 列表（丢弃非法行）。"""
    out: list[EodSnapshotRow] = []
    for raw in raw_rows:
        row = parse_eod_snapshot_row(raw)
        if row is not None:
            out.append(row)
    return out


async def _fetch_one_page(
    client: httpx.AsyncClient,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """拉取单页；逐主机尝试，网络异常重试 _MAX_PAGE_RETRIES 轮，耗尽抛 SnapshotProviderError。

    主机候选见 ``EASTMONEY_CLIST_HOSTS``：单个主机被拒（部分网络会直接断开连接）
    时自动切换下一个，避免因单一域名不可达导致整条盘后链路失效。
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_PAGE_RETRIES + 1):
        for host in EASTMONEY_CLIST_HOSTS:
            try:
                resp = await client.get(
                    f"https://{host}{_CLIST_PATH}",
                    params={
                        "pn": page,
                        "pz": page_size,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f3",
                        "fs": A_SHARE_FILTER,
                        "fields": FIELDS,
                    },
                    timeout=15.0,
                )
                resp.raise_for_status()
                return resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning(
                    "Eastmoney snapshot host=%s 第 %d 页 第 %d 轮失败: %s",
                    host, page, attempt, exc,
                )
        await asyncio.sleep(min(_PAGE_RETRY_BASE_DELAY * (2 ** (attempt - 1)), 8.0))
    raise SnapshotProviderError(
        f"Eastmoney snapshot 第 {page} 页在 {_MAX_PAGE_RETRIES} 轮重试后仍失败: {last_exc}"
    )


async def fetch_full_a_share_snapshot(
    client: httpx.AsyncClient,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """分页拉取全市场 A 股 snapshot（原始 diff 列表）。

    fail-closed：
    - 每页网络异常重试；耗尽抛 SnapshotProviderError。
    - payload 非法（无 data dict / 无 total）抛 SnapshotProviderError。
    - 拉取行数 < 期望 total 抛 SnapshotProviderError（禁止把中间失败伪装成「只有这么多」）。

    返回原始 ``diff`` 字典列表，由调用方负责归一化（保持本模块网络/解析边界清晰）。
    """
    page = 1
    rows: list[dict[str, Any]] = []
    expected_total: int | None = None

    while True:
        payload = await _fetch_one_page(client, page, page_size)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SnapshotProviderError(f"Eastmoney snapshot 非法 payload page={page}")

        if expected_total is None:
            expected_total = int(data.get("total") or 0)
            if expected_total <= 0:
                raise SnapshotProviderError("Eastmoney snapshot 返回非法 total")

        current = data.get("diff") or []
        if not current:
            break

        rows.extend(current)

        if len(rows) >= expected_total:
            break

        page += 1
        if max_pages is not None and page > max_pages:
            break

    if expected_total is None or len(rows) < expected_total:
        raise SnapshotProviderError(
            f"Eastmoney snapshot 不完整: rows={len(rows)}, expected={expected_total}"
        )

    return rows


# ===== 历史日线 fallback（不复权 fqt=0） =====


def _eastmoney_secid(symbol: str, market: str) -> str:
    """Eastmoney secid = '<market_prefix>.<symbol>'。"""
    prefix = _EM_SECID_MARKET.get(market)
    if prefix is None:
        raise ValueError(f"unsupported market for eastmoney secid: {market}")
    return f"{prefix}.{symbol}"


async def fetch_eastmoney_daily_kline(
    client: httpx.AsyncClient,
    symbol: str,
    market: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """拉取单只标的日线（fqt=0 不复权），返回归一化记录列表。

    字段（fields2）：f51 日期, f52 开, f53 收, f54 高, f55 低, f56 量(手), f57 额(元)。
    Eastmoney kline 价格直接以浮点返回（不经过 fltt×100），无需缩放。

    Raises:
        SnapshotProviderError: 网络失败或该标的无数据。
    """
    params = {
        "secid": _eastmoney_secid(symbol, market),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "beg": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "rtntype": "6",
        "klt": "101",   # 日线
        "fqt": "0",     # 关键：不复权
    }
    last_exc: Exception | None = None
    empty_hosts: list[str] = []
    for attempt in range(1, _MAX_PAGE_RETRIES + 1):
        for host in EASTMONEY_HIS_HOSTS:
            try:
                resp = await client.get(
                    f"https://{host}{_KLINE_PATH}", params=params, timeout=15.0
                )
                resp.raise_for_status()
                payload = resp.json()
                data = payload.get("data")
                klines = (data or {}).get("klines") or []
                if not klines:
                    # 主机可达但不提供历史 K 线（部分镜像如此）→ 继续尝试下一个主机，
                    # 不要在第一台上就判定「无数据」。
                    empty_hosts.append(host)
                    logger.warning(
                        "Eastmoney kline host=%s 返回空 klines symbol=%s", host, symbol
                    )
                    continue
                return _parse_kline_records(klines)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning(
                    "Eastmoney kline host=%s %s 第 %d 轮失败: %s",
                    host, symbol, attempt, exc,
                )
        if empty_hosts and last_exc is None:
            # 所有主机均可达但都无数据 → 该标的确实无历史（不能伪装成功）
            break
        await asyncio.sleep(min(_PAGE_RETRY_BASE_DELAY * (2 ** (attempt - 1)), 8.0))

    if last_exc is None and empty_hosts:
        raise SnapshotProviderError(
            f"Eastmoney kline 无数据 symbol={symbol} market={market} {start}~{end}"
            f"（已尝试主机 {empty_hosts}）"
        )
    raise SnapshotProviderError(
        f"Eastmoney kline {symbol} 在 {_MAX_PAGE_RETRIES} 轮重试后仍失败: {last_exc}"
    )


def _parse_kline_records(klines: list[str]) -> list[dict[str, Any]]:
    """klines 为 'f51,f52,...' 逗号串列表，解析为统一记录。"""
    records: list[dict[str, Any]] = []
    for item in klines:
        parts = item.split(",")
        if len(parts) < 7:
            continue
        try:
            records.append(
                {
                    "datetime": parts[0],   # YYYY-MM-DD
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),  # 手
                    "amount": float(parts[6]),  # 元
                }
            )
        except (ValueError, TypeError):
            continue
    return records
