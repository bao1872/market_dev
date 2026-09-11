"""同花顺「不复权」日线 provider（raw OHLCV + 成交额）。

为什么需要它（实测结论，不是设计偏好）：
    盘后 EOD 修复的原定数据源是东方财富 ``push2his`` ``fqt=0``。实测生产出口：

    - ``push2his.eastmoney.com`` 在连续请求后对本机 IP **硬封**（``RemoteProtocolError``，
      冷却 30s 仍不通）；恢复期内的吞吐实测仅 **~0.2 成功请求/秒**（约 5-14 次成功后被封 ~20s）。
      5293 只标的需要 7 小时以上，且途中随时被再次拉黑。
    - 加 ``fltt``/UA/Referer/分片主机（``N.push2his``）均无效。
    - 网易 ``quotes.money.163.com/service/chddata.html`` 恒返回 502。
    - 腾讯 ``fqkline`` 快（~33 req/s）但**不提供成交额**；新浪日线无成交额且 volume 为「股」以外的口径。

    同花顺 ``d.10jqka.com.cn`` 则同时满足三个硬条件，且经过 A/B 验证：

    1. **不复权**：路径末段 ``00`` 为不复权（``01`` 前复权、``02`` 后复权）。
       实测除权日之前的历史（2026-06-10）与 DB canonical raw 逐位相等：
       600519 ``o=1252.08 c=1275.88``、000001 ``o=11.07 c=11.32``、300750 ``o=395.28 c=388.50``。
       若用 ``01``，同一日会得到 1224.06/1247.86 —— 正是「前复权混进 raw」的污染路径。
    2. **含成交额**（字段 6，单位元）。
    3. **吞吐足够**：并发 3 + 4 次重试实测 99.33% 成功、15.8 req/s（5293 只约 5.6 分钟）。

单位契约（与 ``bars_daily`` canonical 对齐，实测）：
    - ``volume`` 单位为 **股**（同花顺原始返回即股）。DB 侧 600519 2026-09-09
      volume=3_222_611 与该日同花顺股数逐位相等；``amount / volume`` = 1293.5 元/股
      落在当日价格区间内，反证单位就是股。
    - ``amount`` 单位为 **元**。
    两者均无需缩放，直接落库。

字段顺序（与东方财富不同，务必区分）：
    同花顺 ``date, open, high, low, close, volume, amount, turnover, ...``
    东方财富 ``date, open, close, high, low, volume, amount``
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

THS_LINE_URL = "https://d.10jqka.com.cn/v6/line/hs_{symbol}/{adjust}/{name}.js"

# 复权类型：实测 00=不复权，01=前复权，02=后复权。
THS_ADJUST_RAW = "00"
THS_ADJUST_QFQ = "01"
THS_ADJUST_HFQ = "02"

# ``last.js`` 返回最近约 140 个交易日的日线。
THS_LAST_BARS = 140

_DEFAULT_RETRIES = 4
_DEFAULT_BASE_DELAY = 0.4
_MAX_BACKOFF = 5.0

# 站点对无 UA/Referer 的裸请求更易限流；带上浏览器化请求头。
_THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://stockpage.10jqka.com.cn/",
}

_DATA_RE = re.compile(r'"data":"(.*?)"', re.S)

# record 的最小字段数：date,open,high,low,close,volume,amount
_MIN_PARTS = 7


class ThsProviderError(RuntimeError):
    """同花顺日线整体失败（网络重试耗尽）。单只标的 404 不算，返回空列表。"""


def _parse_ths_line_body(body: str) -> list[dict[str, Any]]:
    """解析 ``"data"`` 里的 ``日期,开,高,低,收,量(股),额(元),...;`` 串。

    字段顺序是同花顺的 ``date, open, high, low, close``，与东方财富的
    ``date, open, close, high, low`` **不同**，切勿复用东财的解析函数。
    """
    records: list[dict[str, Any]] = []
    for item in body.split(";"):
        parts = item.split(",")
        if len(parts) < _MIN_PARTS:
            continue
        stamp = parts[0].strip()
        if len(stamp) != 8 or not stamp.isdigit():
            continue
        try:
            records.append(
                {
                    "datetime": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}",
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low": float(parts[3]),
                    "close": float(parts[4]),
                    "volume": float(parts[5]),  # 股（canonical，不缩放）
                    "amount": float(parts[6]),  # 元
                }
            )
        except (ValueError, TypeError):
            continue
    return records


async def _get_line_body(
    client: httpx.AsyncClient,
    symbol: str,
    adjust: str,
    name: str,
    *,
    retries: int,
    base_delay: float,
) -> tuple[str | None, str | None]:
    """取一个 ``.js`` 文件的 ``data`` 字段。

    Returns:
        (body, error)。``body is None`` 时 ``error`` 说明原因；
        ``error == "http404"`` 表示该代码/文件不存在（调用方不应重试风暴）。
    """
    url = THS_LINE_URL.format(symbol=symbol, adjust=adjust, name=name)
    last_error: str | None = None
    for attempt in range(retries):
        try:
            resp = await client.get(url, headers=_THS_HEADERS, timeout=15.0)
            if resp.status_code == 404:
                return None, "http404"
            if resp.status_code == 200:
                match = _DATA_RE.search(resp.text)
                if match:
                    return match.group(1), None
                return None, "no-data-field"
            last_error = f"http{resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - 任意网络异常都计入重试
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries - 1:
            await asyncio.sleep(min(base_delay * (2**attempt), _MAX_BACKOFF))
    return None, last_error or "retry-exhausted"


def _needs_year_files(present: list[dict[str, Any]], start: date | None) -> bool:
    """``last.js`` 只覆盖最近 140 根；请求窗口更早时需要按年文件补齐。"""
    if start is None or not present:
        return False
    earliest = min(str(r["datetime"]) for r in present)
    return start.isoformat() < earliest


async def fetch_ths_raw_daily(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
    *,
    adjust: str = THS_ADJUST_RAW,
    retries: int = _DEFAULT_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
) -> list[dict[str, Any]]:
    """拉取 ``[start, end]`` 的同花顺日线（默认不复权），按日期升序返回。

    先取 ``last.js``（最近 ~140 根，覆盖日常修复）；若请求窗口早于其最早日期，
    再按年请求 ``{year}.js`` 补齐。所有记录去重后按 ``[start, end]`` 过滤。

    Raises:
        ThsProviderError: 所有请求均失败（网络层）。单只标的「无数据」返回空列表。
    """
    bodies: list[str] = []
    errors: list[str] = []

    body, error = await _get_line_body(
        client, symbol, adjust, "last", retries=retries, base_delay=base_delay
    )
    if body is not None:
        bodies.append(body)
    elif error and error != "http404":
        errors.append(f"last:{error}")
    elif error == "http404":
        return []

    records = _parse_ths_line_body(body) if body is not None else []

    if _needs_year_files(records, start):
        for year in range(start.year, end.year + 1):
            year_body, year_error = await _get_line_body(
                client, symbol, adjust, str(year), retries=retries, base_delay=base_delay
            )
            if year_body is not None:
                bodies.append(year_body)
            elif year_error and year_error != "http404":
                errors.append(f"{year}:{year_error}")

    if not bodies:
        raise ThsProviderError(
            f"同花顺日线全部失败 symbol={symbol} {start}~{end}: {'; '.join(errors)}"
        )

    merged: dict[str, dict[str, Any]] = {}
    for raw_body in bodies:
        for record in _parse_ths_line_body(raw_body):
            merged[str(record["datetime"])] = record

    lo, hi = start.isoformat(), end.isoformat()
    return sorted(
        (r for stamp, r in merged.items() if lo <= stamp <= hi),
        key=lambda r: str(r["datetime"]),
    )


__all__ = [
    "THS_ADJUST_HFQ",
    "THS_ADJUST_QFQ",
    "THS_ADJUST_RAW",
    "THS_LAST_BARS",
    "THS_LINE_URL",
    "ThsProviderError",
    "fetch_ths_raw_daily",
]
