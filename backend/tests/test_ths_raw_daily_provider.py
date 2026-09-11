"""[THS-RAW] 同花顺「不复权」日线 provider 单元测试（纯单元，mock HTTP）。

为什么必须锁死这些契约：
``d.10jqka.com.cn`` 是 09-10 缺口修复的唯一可用批量源，而它的字段顺序与东方财富
**不同**（``date, open, high, low, close`` vs 东财 ``date, open, close, high, low``）。
若解析错位，会把 ``low`` 写进 ``close``、``amount`` 写进 ``volume``，
产出「看起来成功但整表污染」的数据 —— 这正是最危险的失败模式。

覆盖：
1. 字段顺序（OHLC 不得错位）；
2. 复权路径码 ``00`` = 不复权（默认），``01`` 前复权 / ``02`` 后复权仅显式指定才用；
3. 单位：volume = 股、amount = 元，不缩放；
4. ``last.js`` + 按年 ``{year}.js`` 补齐与去重、按窗口过滤；
5. 404 → 空列表（不是异常）；网络失败重试耗尽 → ThsProviderError。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services import ths_raw_daily_provider as ths


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _ScriptedClient:
    """按 URL 后缀返回脚本化响应；记录请求 URL 与请求头。"""

    def __init__(self, script: dict[str, Any]) -> None:
        self._script = script
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def get(self, url: str, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        for suffix, item in self._script.items():
            if url.endswith(suffix):
                if isinstance(item, Exception):
                    raise item
                return item
        return _FakeResponse(404, "not found")


def _body(rows: list[str]) -> str:
    """构造同花顺 ``last.js`` 的响应体（只需 ``data`` 字段可被正则捕获）。"""
    return 'quotebridge_v6_line_hs_600519_00_last({"data":"' + ";".join(rows) + ';"})'


# 同花顺字段序：date, open, high, low, close, volume(股), amount(元), ...
SH_ROW_1 = "20260908,1290.00,1300.00,1285.00,1295.00,1800000,2331000000.00,1.20"
SH_ROW_2 = "20260909,1291.00,1299.99,1282.00,1294.99,3222611,4171000000.00,1.20"
SH_ROW_3 = "20260910,1291.00,1294.99,1282.00,1285.13,1890022,2428698800.00,1.20"


@pytest.mark.asyncio
async def test_parse_field_order_is_ths_not_eastmoney() -> None:
    """OHLC 必须按同花顺序解析：open, high, low, close。

    用一组「若错位就会明显不同」的值锁死顺序：high 最大、low 最小。
    """
    client = _ScriptedClient({"00/last.js": _FakeResponse(200, _body([SH_ROW_3]))})

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 10), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert len(records) == 1
    rec = records[0]
    assert rec["datetime"] == "2026-09-10"
    assert rec["open"] == 1291.00
    assert rec["high"] == 1294.99
    assert rec["low"] == 1282.00
    assert rec["close"] == 1285.13


@pytest.mark.asyncio
async def test_volume_is_shares_and_amount_is_yuan_no_scaling() -> None:
    """单位契约：volume = 股（不 ×100 也不 ÷100）、amount = 元。"""
    client = _ScriptedClient({"00/last.js": _FakeResponse(200, _body([SH_ROW_2]))})

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 9), date(2026, 9, 9)  # type: ignore[arg-type]
    )

    assert records[0]["volume"] == 3222611.0     # 股（DB canonical 口径）
    assert records[0]["amount"] == 4171000000.0  # 元


@pytest.mark.asyncio
async def test_default_adjust_is_raw_00() -> None:
    """默认必须是 ``/00/``（不复权）；否则会把前复权价格写进 raw 表。"""
    assert ths.THS_ADJUST_RAW == "00"
    client = _ScriptedClient({"00/last.js": _FakeResponse(200, _body([SH_ROW_3]))})

    await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 10), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert any("/hs_600519/00/" in u for u in client.urls)
    assert not any("/hs_600519/01/" in u for u in client.urls)


@pytest.mark.asyncio
async def test_explicit_qfq_path_when_requested() -> None:
    """显式传入前复权码时才走 ``/01/``（不得成为默认）。"""
    client = _ScriptedClient({"01/last.js": _FakeResponse(200, _body([SH_ROW_3]))})

    await ths.fetch_ths_raw_daily(
        client,
        "600519",
        date(2026, 9, 10),
        date(2026, 9, 10),
        adjust=ths.THS_ADJUST_QFQ,  # type: ignore[arg-type]
    )

    assert any("/hs_600519/01/" in u for u in client.urls)


@pytest.mark.asyncio
async def test_browser_headers_sent() -> None:
    """站点对裸请求更易限流；必须带 UA + Referer。"""
    client = _ScriptedClient({"00/last.js": _FakeResponse(200, _body([SH_ROW_3]))})

    await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 10), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert client.headers
    assert "User-Agent" in client.headers[0]
    assert client.headers[0]["Referer"] == "https://stockpage.10jqka.com.cn/"


@pytest.mark.asyncio
async def test_filters_to_requested_window() -> None:
    """只返回 [start, end] 内的记录（last.js 覆盖 ~140 根，必须裁剪）。"""
    client = _ScriptedClient(
        {"00/last.js": _FakeResponse(200, _body([SH_ROW_1, SH_ROW_2, SH_ROW_3]))}
    )

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 9), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert [r["datetime"] for r in records] == ["2026-09-09", "2026-09-10"]


@pytest.mark.asyncio
async def test_year_files_fetch_when_window_precedes_last_js() -> None:
    """请求窗口早于 ``last.js`` 最早日期时，必须补拉按年文件。"""
    year_rows = ["20260105,1200.00,1210.00,1190.00,1205.00,1000000,1205000000.00,1.0"]
    client = _ScriptedClient(
        {
            "00/last.js": _FakeResponse(200, _body([SH_ROW_3])),
            "00/2026.js": _FakeResponse(200, _body(year_rows)),
        }
    )

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 1, 5), date(2026, 1, 5)  # type: ignore[arg-type]
    )

    assert [r["datetime"] for r in records] == ["2026-01-05"]
    assert any("00/2026.js" in u for u in client.urls)


@pytest.mark.asyncio
async def test_year_files_dedup_with_last_js() -> None:
    """按年文件与 last.js 重叠部分必须去重（同一日期只保留一条）。"""
    client = _ScriptedClient(
        {
            "00/last.js": _FakeResponse(200, _body([SH_ROW_2, SH_ROW_3])),
            "00/2026.js": _FakeResponse(200, _body([SH_ROW_3])),
        }
    )

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 8), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    stamps = [r["datetime"] for r in records]
    assert stamps == ["2026-09-09", "2026-09-10"]
    assert len(stamps) == len(set(stamps))


@pytest.mark.asyncio
async def test_404_returns_empty_list_not_exception() -> None:
    """代码不存在（退市/非股票）→ 空列表，不是异常（不得让整批修复中断）。"""
    client = _ScriptedClient({})  # 一切 404

    records = await ths.fetch_ths_raw_daily(
        client, "999999", date(2026, 9, 10), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert records == []


@pytest.mark.asyncio
async def test_network_failure_raises_after_retries() -> None:
    """网络层全部失败 → ThsProviderError（调用方据此换源），不得伪装成空数据。"""
    client = _ScriptedClient({"00/last.js": RuntimeError("connection reset")})

    with pytest.raises(ths.ThsProviderError, match="同花顺日线全部失败"):
        await ths.fetch_ths_raw_daily(
            client,  # type: ignore[arg-type]
            "600519",
            date(2026, 9, 10),
            date(2026, 9, 10),
            retries=2,
            base_delay=0.0,
        )


@pytest.mark.asyncio
async def test_malformed_rows_are_skipped() -> None:
    """脏行（字段不足 / 非数字 / 日期格式错）必须跳过，不得污染结果。"""
    body = _body(
        [
            "not-a-date,1,2,3,4,5,6,7",
            "20260910,abc,1,2,3,4,5,6",
            "20260910",
            SH_ROW_3,
        ]
    )
    client = _ScriptedClient({"00/last.js": _FakeResponse(200, body)})

    records = await ths.fetch_ths_raw_daily(
        client, "600519", date(2026, 9, 10), date(2026, 9, 10)  # type: ignore[arg-type]
    )

    assert len(records) == 1
    assert records[0]["close"] == 1285.13


def test_year_file_needed_only_when_start_precedes_earliest() -> None:
    """年文件触发条件：请求起点早于 last.js 最早日期。"""
    present = [{"datetime": "2026-09-01"}, {"datetime": "2026-09-10"}]
    assert ths._needs_year_files(present, date(2026, 1, 1)) is True  # noqa: SLF001
    assert ths._needs_year_files(present, date(2026, 9, 5)) is False  # noqa: SLF001
    assert ths._needs_year_files(present, None) is False  # noqa: SLF001
    assert ths._needs_year_files([], date(2026, 1, 1)) is False  # noqa: SLF001
