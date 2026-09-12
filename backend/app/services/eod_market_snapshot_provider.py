"""东方财富全市场收盘快照 Provider（非常薄、可测试、不复权）。

职责：
- 通过东方财富 push2 ``clist/get`` 接口分页拉取全市场 A 股收盘快照（盘后）。
- 归一化为项目标准 :class:`EodSnapshotRow`（raw OHLCV，不复权）。Eastmoney 原始 f5 以
  「手」为单位，归一化时按 :data:`SHARES_PER_LOT` 乘 100 转成 canonical **股**；
  ``EodSnapshotRow.volume`` 单位统一为**股**，amount 单位为元。
- 不做任何复权、不做任何指标计算。

fail-closed 契约（详见各函数 docstring）：
- **一个 snapshot 必须来自同一个 host**：禁止「第 1 页来自延时源、第 2 页来自实时源」的跨 host
  拼接。任一页失败 → 整个 host 的快照作废，从下一个 host 的第 1 页重新开始。
- 分页中间页网络异常必须重试；最终失败则整体 fail，禁止 ``except: break`` 半截返回。
- ``total`` 与实拉行数不一致必须 fail-closed；分页过程中 ``total`` 变化同样 fail-closed。
- ``trade_date`` 必须来自 ``f124`` 时间戳（Asia/Shanghai），老时间戳/停牌/空价不伪造 K 线。
- **市场 watermark 必须 >= 15:00**：只有「provider 返回的数据里最大 f124 已过收盘」才能证明
  这是收盘终值。本机 wall-clock 到了 15:05 并不能证明 provider 数据已经到 15:00
  （``push2delay`` 等延时源在 15:05 仍可能只给到 14:50）。

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
    "f5",    # volume 手 → 入库前 ×100 转股（SHARES_PER_LOT）
    "f6",    # amount 元
    "f18",   # previous close
    "f124",  # update timestamp（秒）
])

# clist 的 pz 上限实测为 100；超过该值会被静默截断，故显式使用 100。
DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_RETRIES = 3
_PAGE_RETRY_BASE_DELAY = 1.0

# 【单位契约，实测确定】bars_daily（含 bars_15min/60min 之外的日线）的 canonical
# volume 单位是 **股**，不是手。证据（见 A/B 实测）：
#   - bars_daily 600519 2026-09-09 volume = 3_222_611；同花顺 `/00/`（不复权）该日
#     成交量 = 3_222_611 股，两者逐位相等；000001 / 300750 同样相等。
#   - amount / volume = 4_168_500_480 / 3_222_611 = 1293.5 元/股，与该日
#     [1286.68, 1309.30] 的价格区间吻合 → volume 必为「股」。若按「手」解释，
#     隐含均价会是 129 万/股。
# 而东方财富的 f5（clist）与 f56（kline）实测为 **手**（f6 ≈ f5 × 100 × price），
# 因此入库前必须 ×100 转成股，否则整列会缩小 100 倍。
SHARES_PER_LOT = Decimal("100")

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 当日快照最早可视为「收盘终值」的时刻：收盘 15:00 后留 5 分钟结算余量。
# 该守卫的唯一目的是防止盘前/盘中把实时价写成当日最终日线。
#
# 注意：这是**本机 wall-clock** 守卫，单独使用不足以证明 provider 数据已收盘
# （延时源在 15:05 仍可能只给到 14:50）。必须与
# :func:`validate_snapshot_market_watermark` 的数据 watermark 一起使用。
_EOD_READY_TIME = time(15, 5)

# 数据 watermark 必须达到的真实收盘时刻（A 股 15:00）。
_MARKET_CLOSE_TIME = time(15, 0)

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

    ``volume`` 单位统一为 canonical **股**（Eastmoney 原始 f5 以「手」返回，
    已按 :data:`SHARES_PER_LOT` 在 provider boundary 转股），amount 单位为元。

    ``updated_at`` 保留 f124 的**完整时间**（Asia/Shanghai），而不是只保留日期：
    只有完整时间才能构成 market watermark（判断 provider 数据是否真的已过 15:00）。
    ``trade_date`` 是 ``updated_at`` 的日期投影；无法判定时为 None（不伪造 K 线）。
    """

    symbol: str
    name: str
    market: str                 # SH / SZ / BJ
    updated_at: datetime | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None      # 股（canonical，见 SHARES_PER_LOT）
    amount: Decimal | None      # 元
    previous_close: Decimal | None

    @property
    def trade_date(self) -> date | None:
        """f124 的 Asia/Shanghai 日期投影（无法判定时为 None）。"""
        return self.updated_at.date() if self.updated_at is not None else None


@dataclass(frozen=True)
class SnapshotFetchBatch:
    """一次「完整全市场 snapshot 拉取」的结果（含来源与 watermark 元数据）。

    EOD 与盘中（realtime）两条链路共用同一个分页/failover 实现，只是：
    - ``require_eod_watermark`` 不同（EOD 要求 >= 15:00，盘中不要求）；
    - ``hosts`` 不同（EOD 允许延时源，盘中**只允许实时源**）。

    元数据（source_host / captured_at / market_watermark）供上层做 freshness 与
    数据源质量诊断，避免上层再用「服务器列表首项」伪造来源。
    """

    raw_rows: tuple[dict[str, Any], ...]
    source_host: str
    captured_at: datetime
    market_watermark: datetime | None

    @property
    def raw_count(self) -> int:
        return len(self.raw_rows)


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


def snapshot_updated_at(ts: Any) -> datetime | None:
    """从 f124 时间戳（秒）转为 Asia/Shanghai **完整时间**。fail-closed。

    返回 None 的情形：空 / 非整数 / <=0 / 转换异常。
    """
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=_SHANGHAI_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def snapshot_trade_date(ts: Any) -> date | None:
    """从 f124 时间戳（秒）转为 Asia/Shanghai 日期（:func:`snapshot_updated_at` 的投影）。"""
    updated_at = snapshot_updated_at(ts)
    return updated_at.date() if updated_at is not None else None


def compute_snapshot_market_watermark(
    raw_rows: Sequence[dict[str, Any]],
    *,
    trade_date: date | None = None,
) -> datetime | None:
    """纯计算：取快照中最大的有效 f124（Asia/Shanghai），无有效值返回 None。

    与 :func:`validate_snapshot_market_watermark` 的区别：本函数**不做任何收盘判定**，
    只是把「这批数据的市场时间戳水位」算出来，供 EOD（要求 >= 15:00）与盘中
    （只要求存在当天水位）两种不同 freshness 合同各自判断。

    Args:
        raw_rows: 原始 ``diff`` 行列表（未归一化，直接读 f124）。
        trade_date: 只统计该交易日的时间戳；None 表示不限日期。

    Returns:
        最大 f124 时间；无任何有效时间戳时返回 None。
    """
    timestamps: list[datetime] = []

    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue

        dt = snapshot_updated_at(raw.get("f124"))
        if dt is None:
            continue

        if trade_date is not None and dt.date() != trade_date:
            continue

        timestamps.append(dt)

    return max(timestamps) if timestamps else None


def validate_snapshot_market_watermark(
    raw_rows: Sequence[dict[str, Any]],
    trade_date: date,
) -> datetime:
    """校验快照数据 watermark：目标交易日的最大 f124 必须 >= 15:00。

    为什么不能只看本机时间：``push2delay`` 等延时源在 15:05 仍可能只返回到 14:50，
    那时所有价格都是盘中值。用**数据自身的最大时间戳**判断才能证明这是收盘终值。

    Args:
        raw_rows: 原始 ``diff`` 行列表（未归一化，直接读 f124）。
        trade_date: 目标交易日（Asia/Shanghai）。

    Returns:
        该交易日的 watermark 时间。

    Raises:
        SnapshotProviderError: 没有任何目标日期的有效时间戳，或 watermark < 15:00。
    """
    watermark = compute_snapshot_market_watermark(
        raw_rows,
        trade_date=trade_date,
    )

    if watermark is None:
        raise SnapshotProviderError(
            f"snapshot has no valid timestamps for {trade_date}"
        )

    if watermark.time() < _MARKET_CLOSE_TIME:
        raise SnapshotProviderError(
            f"snapshot is not final: trade_date={trade_date}, "
            f"watermark={watermark.isoformat()}"
        )
    return watermark


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


def _lots_to_shares(value: Any) -> Decimal | None:
    """东方财富成交量（手）→ bars_daily canonical 单位（股）。

    见 :data:`SHARES_PER_LOT` 的实测证据。空/非法返回 None。
    """
    lots = _parse_decimal(value)
    if lots is None:
        return None
    return lots * SHARES_PER_LOT


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
        updated_at=snapshot_updated_at(raw.get("f124")),
        open=_parse_price(raw.get("f17")),
        high=_parse_price(raw.get("f15")),
        low=_parse_price(raw.get("f16")),
        close=_parse_price(raw.get("f2")),
        volume=_lots_to_shares(raw.get("f5")),
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


async def _fetch_page_from_host(
    client: httpx.AsyncClient,
    host: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """从**指定 host** 拉取单页；网络异常重试 _MAX_PAGE_RETRIES 次。

    Raises:
        SnapshotProviderError: 该 host 在该页上重试耗尽。
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_PAGE_RETRIES + 1):
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
        f"Eastmoney snapshot host={host} 第 {page} 页重试 {_MAX_PAGE_RETRIES} 轮后仍失败: "
        f"{last_exc}"
    )


async def _fetch_full_snapshot_from_host(
    client: httpx.AsyncClient,
    host: str,
    *,
    page_size: int,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """从**单一 host** 拉完整分页 snapshot（禁止跨 host 拼接）。

    契约：
    - 全程只用 ``host``，任意一页失败即整体抛错（由外层换 host 从头再来）。
    - 首页确定的 ``total`` 在后续所有页必须保持不变，否则 fail-closed
      （分页期间 total 变化说明数据在流动，拼出来的不是一个一致的市场切片）。
    - 最终 ``len(rows) < expected_total`` 必须 fail-closed。

    Raises:
        SnapshotProviderError: payload 非法 / total 非法或漂移 / 行数不足。
    """
    page = 1
    rows: list[dict[str, Any]] = []
    expected_total: int | None = None

    while True:
        payload = await _fetch_page_from_host(client, host, page, page_size)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SnapshotProviderError(
                f"invalid snapshot payload host={host} page={page}"
            )

        total = int(data.get("total") or 0)
        if total <= 0:
            raise SnapshotProviderError(f"invalid total host={host} page={page}: {total}")

        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise SnapshotProviderError(
                f"snapshot total changed during pagination: host={host}, "
                f"expected={expected_total}, got={total}"
            )

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
            f"incomplete snapshot host={host}: rows={len(rows)}, expected={expected_total}"
        )

    return rows


async def fetch_a_share_snapshot_batch(
    client: httpx.AsyncClient,
    *,
    hosts: Sequence[str],
    expected_trade_date: date | None = None,
    require_eod_watermark: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
    captured_at: datetime | None = None,
) -> SnapshotFetchBatch:
    """通用全市场 snapshot 拉取：单个 host 完成整批 + host failover + watermark 判定。

    EOD 与盘中（realtime）两条链路共用本函数，差异只由入参表达：
    - ``hosts``：EOD 传 :data:`EASTMONEY_CLIST_HOSTS`（允许延时源）；
      盘中必须传实时源（见 ``realtime_market_snapshot_provider.REALTIME_CLIST_HOSTS``），
      **绝不允许**把 ``push2delay`` 放进盘中 host 列表。
    - ``require_eod_watermark``：True 表示要求 watermark >= 15:00（收盘终值合同）；
      False 表示只计算水位、不判收盘（盘中合同）。

    契约（与既有 EOD 行为一致，不新建一套分页逻辑）：
    - 整批只用一个 host；该 host 任一页失败即换下一个 host **从第 1 页重新开始**。
    - ``total`` 漂移 / 行数不足 / payload 非法 → 该 host 作废。
    - 所有 host 都失败 → fail-closed（禁止半截市场或跨 host 拼接）。

    Args:
        client: httpx 异步客户端。
        hosts: 允许使用的 host 列表（按序尝试）。
        expected_trade_date: EOD watermark 校验的目标交易日。
        require_eod_watermark: 是否要求收盘 watermark。
        page_size: 单页条数（clist 实测上限 100）。
        max_pages: 最多拉取页数（None 表示不限）。
        captured_at: 抓取时刻（None 取当前上海时间）；结构化输出用于 freshness 诊断。

    Returns:
        :class:`SnapshotFetchBatch`（含 raw_rows / source_host / captured_at / market_watermark）。

    Raises:
        SnapshotProviderError: host 列表为空、参数不自洽、或所有 host 均失败。
    """
    if not hosts:
        raise SnapshotProviderError("snapshot host list is empty")

    # 参数不自洽属于**调用侧错误**：必须在任何 host / 网络请求之前 fail-fast。
    # 否则会先白白拉完整市场（~53 页 × N hosts）才发现参数错了。
    if require_eod_watermark and expected_trade_date is None:
        raise SnapshotProviderError(
            "expected_trade_date required for EOD watermark validation"
        )

    capture_time = captured_at or datetime.now(_SHANGHAI_TZ)
    if capture_time.tzinfo is None:
        capture_time = capture_time.replace(tzinfo=_SHANGHAI_TZ)
    else:
        capture_time = capture_time.astimezone(_SHANGHAI_TZ)

    errors: list[str] = []

    for host in hosts:
        try:
            rows = await _fetch_full_snapshot_from_host(
                client,
                host,
                page_size=page_size,
                max_pages=max_pages,
            )

            watermark: datetime | None
            if require_eod_watermark:
                eod_trade_date = expected_trade_date
                if eod_trade_date is None:
                    # 理论不可达：入口处已 fail-fast。保留以防未来重构破坏该不变量，
                    # 且此处**绝不**静默退化为「不校验收盘 watermark」。
                    raise SnapshotProviderError(
                        "expected_trade_date required for EOD watermark validation"
                    )
                watermark = validate_snapshot_market_watermark(
                    rows,
                    eod_trade_date,
                )
            else:
                watermark = compute_snapshot_market_watermark(rows)

            return SnapshotFetchBatch(
                raw_rows=tuple(rows),
                source_host=host,
                captured_at=capture_time,
                market_watermark=watermark,
            )

        except SnapshotProviderError as exc:
            errors.append(f"{host}: {exc}")
            logger.warning("snapshot host failover host=%s error=%s", host, exc)

    raise SnapshotProviderError(
        "all snapshot hosts failed: " + "; ".join(errors)
    )


async def fetch_full_a_share_snapshot(
    client: httpx.AsyncClient,
    *,
    expected_trade_date: date | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """分页拉取全市场 A 股 snapshot（原始 diff 列表）——EOD 兼容 wrapper。

    行为与重构前完全一致：使用 :data:`EASTMONEY_CLIST_HOSTS`，且在
    ``expected_trade_date`` 给定时校验收盘 watermark（>= 15:00）。

    需要 host / captured_at / watermark 等元数据的新调用方请直接用
    :func:`fetch_a_share_snapshot_batch`。

    Returns:
        原始 ``diff`` 字典列表，由调用方负责归一化。

    Raises:
        SnapshotProviderError: 所有 host 均失败。
    """
    batch = await fetch_a_share_snapshot_batch(
        client,
        hosts=EASTMONEY_CLIST_HOSTS,
        expected_trade_date=expected_trade_date,
        require_eod_watermark=(expected_trade_date is not None),
        page_size=page_size,
        max_pages=max_pages,
    )

    return list(batch.raw_rows)


# ===== 历史日线 fallback（不复权 fqt=0） =====

# fields2 基础列：日期 开 收 高 低 量(手) 额(元)
_KLINE_FIELDS2_BASIC = "f51,f52,f53,f54,f55,f56,f57"
# 扩展列追加：f58 振幅 / f59 涨跌幅 / f60 涨跌额 / f61 换手率。
# 仅用于只读的 factor-event 兼容性实验（Eastmoney preclose vs canonical factor），
# **不进入 canonical 数据写入路径**。
_KLINE_FIELDS2_EXTENDED = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


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
    *,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """拉取单只标的日线（fqt=0 不复权），返回归一化记录列表。

    字段（fields2）：f51 日期, f52 开, f53 收, f54 高, f55 低, f56 量(手→已×100 转股),
    f57 额(元)。
    Eastmoney kline 价格直接以浮点返回（不经过 fltt×100），无需缩放。

    ``extended=True`` 时额外带回 f58 振幅 / f59 涨跌幅 / f60 涨跌额 / f61 换手率，
    只供只读诊断使用（见 daily_gap_repair_service 的 factor-event 实验）；
    这些字段**绝不写入 bars_daily**。

    Raises:
        SnapshotProviderError: 网络失败或该标的无数据。
    """
    params = {
        "secid": _eastmoney_secid(symbol, market),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": _KLINE_FIELDS2_EXTENDED if extended else _KLINE_FIELDS2_BASIC,
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
                return _parse_kline_records(klines, extended=extended)
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


def _parse_kline_records(
    klines: list[str],
    *,
    extended: bool = False,
) -> list[dict[str, Any]]:
    """klines 为 'f51,f52,...' 逗号串列表，解析为统一记录。"""
    min_parts = 11 if extended else 7
    records: list[dict[str, Any]] = []
    for item in klines:
        parts = item.split(",")
        if len(parts) < min_parts:
            continue
        try:
            record: dict[str, Any] = {
                "datetime": parts[0],   # YYYY-MM-DD
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]) * float(SHARES_PER_LOT),  # 手 → 股
                "amount": float(parts[6]),  # 元
            }
            if extended:
                record["amplitude"] = float(parts[7])
                record["change_pct"] = float(parts[8])   # f59 涨跌幅 %
                record["change_amount"] = float(parts[9])  # f60 涨跌额 元
                record["turnover_rate"] = float(parts[10])
            records.append(record)
        except (ValueError, TypeError):
            continue
    return records
