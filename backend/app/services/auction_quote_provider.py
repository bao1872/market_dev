"""竞价最终报价数据源 Provider 协议与实现（[CHANGE-20260731-001] 数据源合同）。

设计原则：
1. 统一 AuctionFinalQuoteProvider 协议，支持多数据源（mootdx/tushare）
2. MootdxAuctionQuoteProvider 使用 pytdx get_security_quotes 批量获取实时行情
3. 09:25:05 Asia/Shanghai 后调用，获取最终集合竞价结果
4. 不把接口返回自动视为真值；字段缺失、停牌、零成交、调用失败写 quality_status/reason_codes
5. 保存原始 source_time 和 raw_payload 用于审计
6. 不新增 AKShare、东方财富混用、代理或 IP 绕过

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_quote_provider
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.pytdx_adapter import MARKET_NAME_TO_CODE, PytdxAdapter

logger = logging.getLogger(__name__)

# pytdx get_security_quotes 单次批量上限（经验值，超过可能超时）
BATCH_SIZE = 80

# 限速：每批之间间隔（秒），防止 pytdx 服务器断连
BATCH_INTERVAL_SECONDS = 0.3

# 涨跌停阈值（含容差，A 股 ±10%，ST ±5%，保守用 9.9%）
LIMIT_UP_THRESHOLD = 9.9
LIMIT_DOWN_THRESHOLD = -9.9


@dataclass(frozen=True)
class AuctionQuoteResult:
    """单只股票的竞价报价结果。"""

    symbol: str
    market: str  # SH/SZ
    # 行情字段（None = 缺失）
    price: float | None
    last_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: float | None  # pytdx vol 字段（手）
    amount: float | None  # pytdx amount 字段（元）
    servertime: str | None
    # 质量标记
    quality_status: str  # ok/suspended/zero_volume/missing_field/api_error/limit_up/limit_down
    reason_codes: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] | None = None
    source_server: str | None = None

    @property
    def is_valid(self) -> bool:
        """quality_status=ok 表示有效报价。"""
        return self.quality_status == "ok"


@runtime_checkable
class AuctionFinalQuoteProvider(Protocol):
    """竞价最终报价数据源协议。"""

    def fetch_auction_quotes(
        self, symbols: list[tuple[str, str]]
    ) -> list[AuctionQuoteResult]:
        """批量获取竞价报价。

        Args:
            symbols: [(symbol, market), ...] market 为 SH/SZ

        Returns:
            AuctionQuoteResult 列表，顺序与输入不一定一致
        """
        ...

    def close(self) -> None:
        """释放资源。"""
        ...


def _classify_quality(
    price: float | None,
    last_close: float | None,
    volume: float | None,
    change_pct: float | None,
) -> tuple[str, list[str]]:
    """根据行情字段判定 quality_status 和 reason_codes。

    Returns:
        (quality_status, reason_codes)
    """
    reasons: list[str] = []

    if price is None or price <= 0:
        return "missing_field", ["price_missing"]

    if volume is not None and volume == 0:
        return "zero_volume", ["volume_zero"]

    if last_close is not None and last_close > 0:
        if change_pct is not None:
            if change_pct >= LIMIT_UP_THRESHOLD:
                return "limit_up", [f"change_pct={change_pct:.2f}%"]
            if change_pct <= LIMIT_DOWN_THRESHOLD:
                return "limit_down", [f"change_pct={change_pct:.2f}%"]

    return "ok", reasons


class MootdxAuctionQuoteProvider:
    """Mootdx/pytdx 竞价报价 Provider。

    使用 PytdxAdapter.get_security_quotes 批量获取实时行情。
    09:25:05 Asia/Shanghai 后调用，获取最终集合竞价匹配价。

    使用方式：
        with MootdxAuctionQuoteProvider() as provider:
            results = provider.fetch_auction_quotes([("600519", "SH"), ("000001", "SZ")])
    """

    def __init__(self, *, batch_size: int = BATCH_SIZE, batch_interval: float = BATCH_INTERVAL_SECONDS) -> None:
        self._adapter = PytdxAdapter()
        self._batch_size = batch_size
        self._batch_interval = batch_interval
        self._connected = False

    def __enter__(self) -> MootdxAuctionQuoteProvider:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _ensure_connected(self) -> None:
        """延迟连接，避免 __init__ 时连接失败。"""
        if not self._connected:
            self._adapter.connect()
            self._connected = True

    def fetch_auction_quotes(
        self, symbols: list[tuple[str, str]]
    ) -> list[AuctionQuoteResult]:
        """批量获取竞价报价。

        Args:
            symbols: [(symbol, market), ...] market 为 SH/SZ

        Returns:
            AuctionQuoteResult 列表
        """
        if not symbols:
            return []

        try:
            self._ensure_connected()
        except Exception as exc:
            logger.error("[AuctionProvider] pytdx 连接失败: %s", exc)
            return [
                AuctionQuoteResult(
                    symbol=sym, market=mkt,
                    price=None, last_close=None, open=None,
                    high=None, low=None, volume=None, amount=None,
                    servertime=None,
                    quality_status="api_error",
                    reason_codes=[f"connect_failed: {type(exc).__name__}"],
                )
                for sym, mkt in symbols
            ]

        results: list[AuctionQuoteResult] = []

        for i in range(0, len(symbols), self._batch_size):
            batch = symbols[i : i + self._batch_size]
            batch_results = self._fetch_batch(batch)
            results.extend(batch_results)
            if i + self._batch_size < len(symbols) and self._batch_interval > 0:
                time.sleep(self._batch_interval)

        return results

    def _fetch_batch(self, batch: list[tuple[str, str]]) -> list[AuctionQuoteResult]:
        """获取一批报价（最多 self._batch_size 只）。"""
        # 转换为 pytdx 格式 [(market_code, code), ...]
        pytdx_stocks: list[tuple[int, str]] = []
        symbol_map: dict[tuple[int, str], tuple[str, str]] = {}

        for sym, mkt in batch:
            if mkt not in MARKET_NAME_TO_CODE:
                logger.warning("[AuctionProvider] 未知市场: %s symbol=%s", mkt, sym)
                continue
            market_code = MARKET_NAME_TO_CODE[mkt]
            pytdx_stocks.append((market_code, sym))
            symbol_map[(market_code, sym)] = (sym, mkt)

        if not pytdx_stocks:
            return []

        try:
            raw_list = self._adapter.api.get_security_quotes(pytdx_stocks)
        except Exception as exc:
            logger.error("[AuctionProvider] get_security_quotes 失败: %s", exc)
            return [
                AuctionQuoteResult(
                    symbol=sym, market=mkt,
                    price=None, last_close=None, open=None,
                    high=None, low=None, volume=None, amount=None,
                    servertime=None,
                    quality_status="api_error",
                    reason_codes=[f"api_call_failed: {type(exc).__name__}: {exc}"],
                )
                for sym, mkt in batch
            ]

        if not raw_list:
            return [
                AuctionQuoteResult(
                    symbol=sym, market=mkt,
                    price=None, last_close=None, open=None,
                    high=None, low=None, volume=None, amount=None,
                    servertime=None,
                    quality_status="api_error",
                    reason_codes=["empty_response"],
                )
                for sym, mkt in batch
            ]

        results: list[AuctionQuoteResult] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            market_code_raw: Any = raw.get("market")
            code_raw: Any = raw.get("code")
            if not isinstance(market_code_raw, int) or not isinstance(code_raw, str):
                continue
            key: tuple[int, str] = (market_code_raw, code_raw)
            sym_mkt = symbol_map.get(key)
            if sym_mkt is None:
                continue
            sym, mkt = sym_mkt

            price = _safe_float(raw.get("price"))
            last_close = _safe_float(raw.get("last_close"))
            open_price = _safe_float(raw.get("open"))
            high = _safe_float(raw.get("high"))
            low = _safe_float(raw.get("low"))
            volume = _safe_float(raw.get("vol"))
            amount = _safe_float(raw.get("amount"))
            servertime = raw.get("servertime")

            change_pct = (
                (price - last_close) / last_close * 100.0
                if price is not None and last_close is not None and last_close > 0
                else None
            )

            quality_status, reason_codes = _classify_quality(
                price, last_close, volume, change_pct
            )

            results.append(
                AuctionQuoteResult(
                    symbol=sym,
                    market=mkt,
                    price=price,
                    last_close=last_close,
                    open=open_price,
                    high=high,
                    low=low,
                    volume=volume,
                    amount=amount,
                    servertime=str(servertime) if servertime is not None else None,
                    quality_status=quality_status,
                    reason_codes=reason_codes,
                    raw_payload=raw,
                    source_server=self._adapter._servers[0][0] if self._adapter._servers else None,
                )
            )

        # 补充未返回的 symbol
        returned_keys = {(r.market, r.symbol) for r in results}
        for sym, mkt in batch:
            if (mkt, sym) not in returned_keys:
                results.append(
                    AuctionQuoteResult(
                        symbol=sym, market=mkt,
                        price=None, last_close=None, open=None,
                        high=None, low=None, volume=None, amount=None,
                        servertime=None,
                        quality_status="api_error",
                        reason_codes=["not_returned_by_api"],
                    )
                )

        return results

    def close(self) -> None:
        """断开 pytdx 连接。"""
        if self._connected:
            self._adapter.disconnect()
            self._connected = False


def _safe_float(v: Any) -> float | None:
    """安全转换为 float，None/非数值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    import os

    if os.environ.get("PURE_UNIT_TEST"):
        # 纯单元测试：不连接网络
        r = _classify_quality(10.5, 10.0, 100, 5.0)
        assert r[0] == "ok", f"expected ok, got {r}"

        r = _classify_quality(11.0, 10.0, 0, 10.0)
        assert r[0] == "zero_volume", f"expected zero_volume, got {r}"

        r = _classify_quality(None, 10.0, 100, None)
        assert r[0] == "missing_field", f"expected missing_field, got {r}"

        r = _classify_quality(11.05, 10.0, 100, 10.5)
        assert r[0] == "limit_up", f"expected limit_up, got {r}"

        r = _classify_quality(8.95, 10.0, 100, -10.5)
        assert r[0] == "limit_down", f"expected limit_down, got {r}"

        print("[PASS] _classify_quality 纯单元测试")

        # Provider 协议检查
        provider = MootdxAuctionQuoteProvider()
        assert isinstance(provider, AuctionFinalQuoteProvider), "MootdxAuctionQuoteProvider 未实现协议"
        print("[PASS] AuctionFinalQuoteProvider 协议检查")
        print("[PASS] 所有模块自测通过")
