"""本地验证 /api/v1/instruments/{id}/quote 的可信化字段。

用法：
    cd /root/web_dev/backend
    APP_ENV=test DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        REDIS_URL=redis://localhost:6379/1 python scripts/verify_quote_trustworthy.py

行为：
1. 在测试库创建临时 instrument + 当日日线 bar。
2. 调用 /quote（当前若为非交易时段，预期 source=daily_fallback, degraded=false）。
3. Monkeypatch 交易时段与 pytdx 返回值，再次调用 /quote
   （预期 source=pytdx, is_realtime=true, degraded=false）。
4. 清理临时数据。
"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

import httpx
from sqlalchemy import text

from app.api import bars as bars_api
from app.core.time import now_shanghai
from app.db import AsyncSessionLocal
from app.main import app
from app.models.bar import BarDaily
from app.models.instrument import Instrument


async def _create_test_data(db):
    symbol = f"VQT{uuid.uuid4().hex[:6].upper()}"
    instrument = Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        market="SH",
        name="Quote Trust Verify",
        status="active",
    )
    db.add(instrument)
    await db.flush()

    today = now_shanghai().date()
    bar = BarDaily(
        instrument_id=instrument.id,
        trade_date=today,
        open=Decimal("10.0"),
        high=Decimal("11.0"),
        low=Decimal("9.5"),
        close=Decimal("10.5"),
        volume=Decimal("100000"),
        amount=Decimal("1050000"),
    )
    db.add(bar)
    await db.flush()
    await db.commit()
    return instrument


async def _cleanup(db, instrument_id):
    await db.execute(text("DELETE FROM bars_daily WHERE instrument_id = :id"), {"id": str(instrument_id)})
    await db.execute(text("DELETE FROM instruments WHERE id = :id"), {"id": str(instrument_id)})
    await db.commit()


async def _call_quote(client: httpx.AsyncClient, instrument_id: uuid.UUID):
    resp = await client.get(f"/api/v1/instruments/{instrument_id}/quote")
    resp.raise_for_status()
    return resp.json()


async def main():
    async with AsyncSessionLocal() as db:
        instrument = await _create_test_data(db)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                print(f"临时 instrument_id: {instrument.id}\n")

                # 1. 非交易时段 / pytdx 失败场景：DB fallback
                print("=== 场景 1：非交易时段 DB fallback（degraded=false） ===")
                data = await _call_quote(client, instrument.id)
                print(f"curl 示例：curl -s http://127.0.0.1:8000/api/v1/instruments/{instrument.id}/quote | python -m json.tool")
                print(f"响应：{data}")
                assert data["source"] == "daily_fallback"
                assert data["is_realtime"] is False
                assert data["degraded"] is False
                print("PASS\n")

                # 2. 模拟交易时段 pytdx 成功
                print("=== 场景 2：交易时段 pytdx 成功（本地 mock） ===")
                now = now_shanghai()
                original_session_check = bars_api._is_quote_realtime_session
                original_fetch = bars_api._fetch_pytdx_quote
                original_cache_get = bars_api._quote_cache_get

                async def _mock_session(*_a, **_kw):
                    return True

                async def _mock_fetch(_symbol):
                    return {
                        "current_price": 10.8,
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.5,
                        "close": 10.8,
                        "volume": 200000.0,
                        "prev_close": 10.5,
                        "change_pct": 2.86,
                        "update_time": now.isoformat(),
                        "is_realtime": True,
                    }

                async def _mock_cache_get(*_a, **_kw):
                    return None

                bars_api._is_quote_realtime_session = _mock_session
                bars_api._fetch_pytdx_quote = _mock_fetch
                bars_api._quote_cache_get = _mock_cache_get

                try:
                    data = await _call_quote(client, instrument.id)
                    print(f"curl 示例：curl -s http://127.0.0.1:8000/api/v1/instruments/{instrument.id}/quote | python -m json.tool")
                    print(f"响应：{data}")
                    assert data["source"] == "pytdx"
                    assert data["is_realtime"] is True
                    assert data["degraded"] is False
                    assert data["freshness_seconds"] >= 0
                    print("PASS\n")
                finally:
                    bars_api._is_quote_realtime_session = original_session_check
                    bars_api._fetch_pytdx_quote = original_fetch
                    bars_api._quote_cache_get = original_cache_get

                # 3. 模拟交易时段 pytdx 失败，触发降级
                print("=== 场景 3：交易时段 pytdx 失败，降级到 daily_fallback（degraded=true） ===")

                async def _mock_session_fail(*_a, **_kw):
                    return True

                async def _mock_fetch_fail(_symbol):
                    return None

                async def _mock_cache_get_fail(*_a, **_kw):
                    return None

                bars_api._is_quote_realtime_session = _mock_session_fail
                bars_api._fetch_pytdx_quote = _mock_fetch_fail
                bars_api._quote_cache_get = _mock_cache_get_fail

                try:
                    data = await _call_quote(client, instrument.id)
                    print(f"curl 示例：curl -s http://127.0.0.1:8000/api/v1/instruments/{instrument.id}/quote | python -m json.tool")
                    print(f"响应：{data}")
                    assert data["source"] == "daily_fallback"
                    assert data["is_realtime"] is False
                    assert data["degraded"] is True
                    assert data["degraded_reason"] is not None
                    print("PASS\n")
                finally:
                    bars_api._is_quote_realtime_session = original_session_check
                    bars_api._fetch_pytdx_quote = original_fetch
                    bars_api._quote_cache_get = original_cache_get
        finally:
            await _cleanup(db, instrument.id)


if __name__ == "__main__":
    asyncio.run(main())
