"""拼音搜索测试 - advice.md 第六节。

测试内容：
1. compute_pinyin_initials 单元测试（核心用例 + 边界）
2. GET /instruments 拼音首字母前缀搜索（dmgf -> 东睦股份）
3. 大小写不敏感（DMGF 与 dmgf 等价）
4. 名称包含搜索（东睦 -> 东睦股份）
5. 代码完全匹配（600114 -> 东睦股份）
6. 搜索优先级排序（代码完全匹配 > 代码前缀 > 拼音前缀 > 名称包含）
7. 响应包含 pinyin_initials 字段

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 含 pinyin_initials 的测试数据通过 instrument_factory 注入
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pinyin_util import compute_pinyin_initials


def _make_instrument_kwargs(symbol: str, name: str, pinyin: str | None, market: str = "SH") -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "pinyin_initials": pinyin,
        "market": market,
        "status": "active",
        "listing_date": date(2001, 8, 27),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


@pytest.fixture
async def pinyin_instruments(db_session: AsyncSession, instrument_factory):
    """预置含 pinyin_initials 的测试标的。"""
    instruments = [
        await instrument_factory(**_make_instrument_kwargs("600114", "东睦股份", "dmgf", "SH")),
        await instrument_factory(**_make_instrument_kwargs("603730", "岱美股份", "dmgf", "SH")),
        await instrument_factory(**_make_instrument_kwargs("600519", "贵州茅台", "gzmt", "SH")),
        await instrument_factory(**_make_instrument_kwargs("000001", "平安银行", "payh", "SZ")),
        # 优先级测试专用：symbol 完全匹配 / 前缀 / pinyin 前缀 同时命中
        await instrument_factory(**_make_instrument_kwargs("dmgf", "优先级A", None, "SH")),
        await instrument_factory(**_make_instrument_kwargs("dmgf2", "优先级B", None, "SH")),
    ]
    return instruments


# ===== 单元测试：compute_pinyin_initials =====


def test_compute_pinyin_initials_core() -> None:
    """核心用例：中文名 -> 小写拼音首字母。"""
    assert compute_pinyin_initials("东睦股份") == "dmgf"
    assert compute_pinyin_initials("贵州茅台") == "gzmt"
    assert compute_pinyin_initials("隆基绿能") == "ljln"
    assert compute_pinyin_initials("平安银行") == "payh"
    assert compute_pinyin_initials("宁德时代") == "ndsd"


def test_compute_pinyin_initials_special_chars() -> None:
    """边界：含符号/字母的名称，符号剔除、字母转小写。"""
    assert compute_pinyin_initials("*ST康美") == "stkm"
    assert compute_pinyin_initials("1000ETF") == "1000etf"


def test_compute_pinyin_initials_empty() -> None:
    """边界：空输入返回 None。"""
    assert compute_pinyin_initials("") is None
    assert compute_pinyin_initials(None) is None
    assert compute_pinyin_initials("   ") is None


# ===== API 测试：拼音搜索 =====


@pytest.mark.asyncio
async def test_search_by_pinyin_prefix(client: AsyncClient, pinyin_instruments) -> None:
    """拼音首字母前缀搜索：dmgf -> 东睦股份 + 岱美股份。"""
    response = await client.get("/instruments", params={"keyword": "dmgf"})
    assert response.status_code == 200
    data = response.json()
    # 命中：东睦股份 + 岱美股份(pinyin 前缀) + 优先级A(symbol 完全匹配) + 优先级B(symbol 前缀)
    assert data["total"] == 4
    symbols = [item["symbol"] for item in data["items"]]
    assert "600114" in symbols  # 东睦股份
    assert "603730" in symbols  # 岱美股份


@pytest.mark.asyncio
async def test_search_by_pinyin_uppercase(client: AsyncClient, pinyin_instruments) -> None:
    """大小写不敏感：DMGF 与 dmgf 等价（pinyin_initials 前缀匹配）。"""
    response = await client.get("/instruments", params={"keyword": "DMGF"})
    assert response.status_code == 200
    data = response.json()
    # DMGF 转小写后匹配 pinyin_initials LIKE 'dmgf%'，命中东睦+岱美；
    # symbol LIKE 'DMGF%'（PostgreSQL ILIKE）命中优先级A/B
    assert data["total"] == 4
    symbols = [item["symbol"] for item in data["items"]]
    assert "600114" in symbols


@pytest.mark.asyncio
async def test_search_by_name_contains(client: AsyncClient, pinyin_instruments) -> None:
    """名称包含搜索：东睦 -> 东睦股份。"""
    response = await client.get("/instruments", params={"keyword": "东睦"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600114"
    assert data["items"][0]["name"] == "东睦股份"


@pytest.mark.asyncio
async def test_search_by_symbol_exact(client: AsyncClient, pinyin_instruments) -> None:
    """代码完全匹配：600114 -> 东睦股份。"""
    response = await client.get("/instruments", params={"keyword": "600114"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600114"
    assert data["items"][0]["name"] == "东睦股份"


@pytest.mark.asyncio
async def test_search_priority_ordering(client: AsyncClient, pinyin_instruments) -> None:
    """搜索优先级：代码完全匹配(0) < 代码前缀(1) < 拼音前缀(2) < 名称包含(3)。

    keyword=dmgf 命中：
    - 优先级A: symbol='dmgf' 完全匹配 -> rank 0
    - 优先级B: symbol='dmgf2' 前缀匹配 -> rank 1
    - 东睦股份/岱美股份: pinyin_initials='dmgf' 前缀 -> rank 2
    """
    response = await client.get("/instruments", params={"keyword": "dmgf", "page_size": 10})
    assert response.status_code == 200
    data = response.json()
    symbols = [item["symbol"] for item in data["items"]]
    # rank 0 (dmgf) 排第一
    assert symbols[0] == "dmgf"
    # rank 1 (dmgf2) 排第二
    assert symbols[1] == "dmgf2"
    # rank 2 (东睦股份 600114 / 岱美股份 603730) 排后，按 symbol 排序 600114 < 603730
    assert symbols[2] == "600114"
    assert symbols[3] == "603730"


@pytest.mark.asyncio
async def test_response_has_pinyin_initials(client: AsyncClient, pinyin_instruments) -> None:
    """响应包含 pinyin_initials 字段。"""
    response = await client.get("/instruments", params={"keyword": "600114"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "pinyin_initials" in item
    assert item["pinyin_initials"] == "dmgf"


@pytest.mark.asyncio
async def test_search_no_match(client: AsyncClient, pinyin_instruments) -> None:
    """边界：无匹配 keyword 返回空。"""
    response = await client.get("/instruments", params={"keyword": "zzzznotexist"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


if __name__ == "__main__":
    # 自测入口：直接运行验证（PostgreSQL 测试库依赖 pytest fixtures）
    pytest.main([__file__, "-v", "--tb=short"])
